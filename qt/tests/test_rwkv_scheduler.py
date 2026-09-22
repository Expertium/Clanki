# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from anki import cards_pb2, collection_pb2, deck_config_pb2, scheduler_pb2
from anki.decks import DeckId, FilteredDeckConfig
from anki.scheduler.v3 import SchedulingState, SchedulingStates
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import (
    RwkvBackendCacheSnapshot,
    RwkvCurveSources,
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewCandidate,
    RwkvReviewerPrediction,
    RwkvReviewerStateSnapshot,
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewPredictionRequest,
    RwkvReviewState,
    RwkvReviewTransition,
    RwkvStatefulReviewerBackend,
    RwkvWarmUpProgress,
    apply_review_interval_overrides,
    configure_reviewer_backend_from_environment,
    current_reviewer_diagnostics,
    current_reviewer_retrievability,
    interval_from_recall_curve,
    prepare_filtered_deck_retrievability_scores,
    prepare_reviewer_queue_order,
    prepare_stats_retrievability_scores,
    prewarm_reviewer_queue_score_cache,
    record_collection_redo,
    record_collection_undo,
    record_reviewer_answer,
    rwkv_card_info_after_review_rows,
    rwkv_card_info_rows,
    rwkv_curve_scheduling_states,
    rwkv_review_enabled,
    rwkv_review_identity,
    rwkv_review_input,
    set_reviewer_backend,
    unrounded_interval_from_recall_curve,
    update_reviewer_scheduling_states,
)
from aqt.rwkv_srs_benchmark import (
    _rust_warmup_chunk_size,
    _workload_snapshot_for_review_inputs,
)


@pytest.fixture(autouse=True)
def reset_rwkv_reviewer_backend() -> Iterator[None]:
    previous = set_reviewer_backend(None)
    previous_backend_assignment_generation = (
        rwkv_scheduler._reviewer_backend_assignment_generation
    )
    previous_warmup_states = dict(rwkv_scheduler._reviewer_backend_warmup_states)
    previous_warmup_generations = dict(
        rwkv_scheduler._reviewer_backend_warmup_generations
    )
    previous_pending_generations = dict(
        rwkv_scheduler._reviewer_backend_warmup_pending_generations
    )
    previous_cold_fallback_generations = dict(
        rwkv_scheduler._reviewer_backend_cold_fallback_generations
    )
    previous_memorised_identity_cache = dict(
        rwkv_scheduler._rwkv_memorised_history_identity_cache
    )
    previous_preset_cache = dict(rwkv_scheduler._resolved_preset_id_cache)
    previous_queue_score_maps = dict(rwkv_scheduler._rwkv_review_queue_score_maps)
    previous_queue_target_maps = dict(rwkv_scheduler._rwkv_review_queue_target_maps)
    previous_queue_score_generations = dict(
        rwkv_scheduler._rwkv_review_queue_score_generations
    )
    previous_queue_score_config_keys = dict(
        rwkv_scheduler._rwkv_review_queue_score_config_keys
    )
    previous_queue_collection_key = rwkv_scheduler._rwkv_review_queue_collection_key
    previous_review_input_generation = rwkv_scheduler._rwkv_review_input_generation
    previous_study_queue_generation = rwkv_scheduler._rwkv_study_queue_generation
    previous_input_batch_cache = (
        rwkv_scheduler._rwkv_review_input_batch_module_cache.copy()
    )
    previous_stats_prepare = dict(rwkv_scheduler._rwkv_stats_prepare_in_flight)
    previous_score_prewarm = set(rwkv_scheduler._rwkv_score_prewarm_in_flight)
    previous_memorised_job = rwkv_scheduler._rwkv_memorised_history_job
    previous_startup_build_started = rwkv_scheduler._rwkv_startup_build_started
    previous_model_cache_signature = rwkv_scheduler._rwkv_model_cache_signature
    previous_model_cache_value = rwkv_scheduler._rwkv_model_cache_value
    rwkv_scheduler._reviewer_backend_warmup_states.clear()
    rwkv_scheduler._reviewer_backend_assignment_generation = 0
    rwkv_scheduler._reviewer_backend_warmup_generations.clear()
    rwkv_scheduler._reviewer_backend_warmup_pending_generations.clear()
    rwkv_scheduler._reviewer_backend_cold_fallback_generations.clear()
    rwkv_scheduler._rwkv_memorised_history_identity_cache.clear()
    rwkv_scheduler._resolved_preset_id_cache.clear()
    rwkv_scheduler._rwkv_review_queue_score_maps.clear()
    rwkv_scheduler._rwkv_review_queue_target_maps.clear()
    rwkv_scheduler._rwkv_review_queue_score_generations.clear()
    rwkv_scheduler._rwkv_review_queue_score_config_keys.clear()
    rwkv_scheduler._rwkv_review_queue_collection_key = None
    rwkv_scheduler._rwkv_review_input_generation = 0
    rwkv_scheduler._rwkv_study_queue_generation = 0
    rwkv_scheduler._rwkv_review_input_batch_module_cache.clear()
    rwkv_scheduler._rwkv_stats_prepare_in_flight.clear()
    rwkv_scheduler._rwkv_score_prewarm_in_flight.clear()
    rwkv_scheduler._rwkv_startup_build_started = False
    rwkv_scheduler._rwkv_model_cache_signature = None
    rwkv_scheduler._rwkv_model_cache_value = None
    rwkv_scheduler._rwkv_memorised_history_job = None
    try:
        yield
    finally:
        rwkv_scheduler._rwkv_memorised_history_job = previous_memorised_job
        rwkv_scheduler._rwkv_model_cache_signature = previous_model_cache_signature
        rwkv_scheduler._rwkv_model_cache_value = previous_model_cache_value
        set_reviewer_backend(previous)
        rwkv_scheduler._reviewer_backend_assignment_generation = (
            previous_backend_assignment_generation
        )
        rwkv_scheduler._reviewer_backend_warmup_states.clear()
        rwkv_scheduler._reviewer_backend_warmup_states.update(previous_warmup_states)
        rwkv_scheduler._reviewer_backend_warmup_generations.clear()
        rwkv_scheduler._reviewer_backend_warmup_generations.update(
            previous_warmup_generations
        )
        rwkv_scheduler._reviewer_backend_warmup_pending_generations.clear()
        rwkv_scheduler._reviewer_backend_warmup_pending_generations.update(
            previous_pending_generations
        )
        rwkv_scheduler._reviewer_backend_cold_fallback_generations.clear()
        rwkv_scheduler._reviewer_backend_cold_fallback_generations.update(
            previous_cold_fallback_generations
        )
        rwkv_scheduler._rwkv_memorised_history_identity_cache.clear()
        rwkv_scheduler._rwkv_memorised_history_identity_cache.update(
            previous_memorised_identity_cache
        )
        rwkv_scheduler._resolved_preset_id_cache.clear()
        rwkv_scheduler._resolved_preset_id_cache.update(previous_preset_cache)
        rwkv_scheduler._rwkv_review_queue_score_maps.clear()
        rwkv_scheduler._rwkv_review_queue_score_maps.update(previous_queue_score_maps)
        rwkv_scheduler._rwkv_review_queue_target_maps.clear()
        rwkv_scheduler._rwkv_review_queue_target_maps.update(previous_queue_target_maps)
        rwkv_scheduler._rwkv_review_queue_score_generations.clear()
        rwkv_scheduler._rwkv_review_queue_score_generations.update(
            previous_queue_score_generations
        )
        rwkv_scheduler._rwkv_review_queue_score_config_keys.clear()
        rwkv_scheduler._rwkv_review_queue_score_config_keys.update(
            previous_queue_score_config_keys
        )
        rwkv_scheduler._rwkv_review_queue_collection_key = previous_queue_collection_key
        rwkv_scheduler._rwkv_review_input_generation = previous_review_input_generation
        rwkv_scheduler._rwkv_study_queue_generation = previous_study_queue_generation
        rwkv_scheduler._rwkv_review_input_batch_module_cache.clear()
        rwkv_scheduler._rwkv_review_input_batch_module_cache.update(
            previous_input_batch_cache
        )
        rwkv_scheduler._rwkv_stats_prepare_in_flight.clear()
        rwkv_scheduler._rwkv_stats_prepare_in_flight.update(previous_stats_prepare)
        rwkv_scheduler._rwkv_score_prewarm_in_flight.clear()
        rwkv_scheduler._rwkv_score_prewarm_in_flight.update(previous_score_prewarm)
        rwkv_scheduler._rwkv_startup_build_started = previous_startup_build_started


def test_rwkv_queue_refresh_due_uses_nested_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": False,
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_interval": 3,
                    }
                },
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())),
        card=SimpleNamespace(id=1, did=100),
        _answeredIds=[1, 2],
    )

    assert not rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)

    reviewer._answeredIds.append(3)

    assert rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)


# Pins spec/deck-options.md#deck-options.rwkv-fixed-settings
def test_rwkv_first_review_elapsed_from_card_creation_is_always_on() -> None:
    for config in (
        {},
        {"rwkvReviewFirstReviewElapsedFromCardCreation": False},
        {
            "other": {
                "jschoreels.rwkv": {
                    "rwkv_review_first_review_elapsed_from_card_creation": False,
                }
            }
        },
    ):
        assert rwkv_scheduler._rwkv_review_first_review_elapsed_from_card_creation(
            config
        )


def test_rwkv_min_intervening_reviews_defaults_to_five_and_allows_zero() -> None:
    assert rwkv_scheduler._rwkv_review_min_intervening_reviews({}) == 5
    assert (
        rwkv_scheduler._rwkv_review_min_intervening_reviews(
            {"rwkvReviewMinInterveningReviews": 0}
        )
        == 0
    )


def test_rwkv_review_batch_size_accepts_8192_and_rejects_larger_values() -> None:
    assert rwkv_scheduler._rwkv_review_batch_size({"rwkvReviewBatchSize": 8192}) == 8192
    assert rwkv_scheduler._rwkv_review_batch_size({"rwkvReviewBatchSize": 8193}) == 512


def test_rwkv_queue_caches_are_scoped_to_collection() -> None:
    first = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    second = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())

    first_input_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=first,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    second_input_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=second,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )

    assert first_input_key is not None
    assert second_input_key is not None
    assert first_input_key != second_input_key

    rwkv_scheduler._set_rwkv_review_queue_scores(first, 100, [(1, 0.25)])
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(first, 100) == {
        1: pytest.approx(0.25)
    }
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(second, 100) is None


def test_study_queue_change_invalidates_cached_and_async_rwkv_work() -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_count_clears = 0

        def clear_rwkv_deck_count_scores(self) -> None:
            self.deck_count_clears += 1

    rpc = Rpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.reviewer = SimpleNamespace()
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.decks.deck_and_child_ids = lambda deck_id: [deck_id]
    reviewer.mw.col.db = SimpleNamespace()
    resident_backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(resident_backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )
    context = rwkv_scheduler._rwkv_review_queue_context(reviewer, 100)
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert context is not None
    assert cache_key is not None

    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={},
        loaded_rows=0,
        parsed_cards=0,
        cards_with_state=0,
        disabled_config_cards=0,
        eligible_cards=0,
        deck_configs=0,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=context,
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=input_build,
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}
    rwkv_scheduler._rwkv_review_queue_target_maps[100] = {1: 0.90}
    rwkv_scheduler._rwkv_review_input_batch_module_cache[cache_key] = input_build
    rwkv_scheduler._rwkv_score_prewarm_in_flight.add((1, 2, 3, 4, (100,)))

    rwkv_scheduler.study_queues_did_change(
        reviewer.mw,
        initiator=object(),
        changes=collection_pb2.OpChanges(config=True, study_queues=True),
    )

    assert rwkv_scheduler._rwkv_study_queue_generation == 1
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rwkv_scheduler._rwkv_review_queue_target_maps == {}
    assert rwkv_scheduler._rwkv_review_input_batch_module_cache == {}
    assert rwkv_scheduler._rwkv_score_prewarm_in_flight == set()
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1
    assert rpc.calls[-1] == {"deck_id": 100, "scores": []}
    assert rpc.deck_count_clears == 1
    assert not rwkv_scheduler.install_reviewer_queue_order_async_result(
        reviewer,
        result,
    )


def test_current_deck_change_preserves_resident_rwkv_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_count_clears = 0

        def clear_rwkv_deck_count_scores(self) -> None:
            self.deck_count_clears += 1

    rpc = Rpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.reviewer = SimpleNamespace()
    reviewer.mw.deckBrowser = deck_browser = object()
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.db = SimpleNamespace()
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}
    refreshed_markers: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_refresh_rwkv_state_cache_collection_mod",
        lambda _reviewer, identity: refreshed_markers.append(identity),
    )

    changes = collection_pb2.OpChanges(config=True, study_queues=True)
    rwkv_scheduler.study_queues_did_change(
        reviewer.mw,
        initiator=deck_browser,
        changes=changes,
    )

    assert rwkv_scheduler._rwkv_study_queue_generation == 1
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] == (
        resident_identity
    )
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] == (
        0,
        resident_identity,
    )
    assert rwkv_scheduler._reviewer_backend_warmup_generations.get(warmup_key, 0) == 0
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rpc.calls[-1] == {"deck_id": 100, "scores": []}
    assert rpc.deck_count_clears == 1
    assert refreshed_markers == [resident_identity]


def test_study_queue_change_invalidates_resolved_preset_ids() -> None:
    class Rpc(_RwkvQueueScoreRpc):
        preset_id = "1000"

        def get_fsrs_preset_ids_for_cards(self, cids: list[int]) -> SimpleNamespace:
            self.preset_id_calls.append(list(cids))
            return SimpleNamespace(
                items=[
                    SimpleNamespace(card_id=card_id, preset_id=self.preset_id)
                    for card_id in cids
                ]
            )

    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer.mw.reviewer = SimpleNamespace()

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1]) == {1: "1000"}
    rpc.preset_id = "2000"
    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=object())

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1]) == {1: "2000"}
    assert rpc.preset_id_calls == [[1], [1]]


def test_preset_resolution_change_invalidates_resident_rwkv_state() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )

    rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1


@pytest.mark.parametrize(
    "nested_editor",
    [False, True],
    ids=["legacy-editor", "new-editor-window"],
)
def test_editor_change_keeps_resident_state_for_undo_restored_card(
    nested_editor: bool,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.get_config = lambda _key: {
        "rules": [{"search": "Front:foo", "preset_id": "1000"}]
    }

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            if "from cards" in sql:
                assert args == (10,)
                return [1]
            assert "from revlog" in sql
            assert args == ()
            return [1]

    reviewer.mw.col.db = DB()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    cache_key = rwkv_scheduler._preset_id_cache_key(reviewer)
    rwkv_scheduler._resolved_preset_id_cache[cache_key] = {1: "1000"}
    rwkv_scheduler._invalidate_reviewer_transient_scores_after_undo(reviewer, [1])
    assert rwkv_scheduler._resolved_preset_id_cache[cache_key] == {}

    identity = rwkv_review_identity(
        reviewer,
        _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
    )
    assert identity is not None
    assert identity.preset_id == 1000
    assert rwkv_scheduler._resolved_preset_id_cache[cache_key] == {1: "1000"}

    editor = SimpleNamespace(nid=10, card=SimpleNamespace(id=1))
    initiator = SimpleNamespace(editor=editor) if nested_editor else editor
    rwkv_scheduler.collection_content_did_change(reviewer.mw, initiator)

    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] == (
        resident_identity
    )
    assert rwkv_scheduler._reviewer_backend_warmup_generations.get(warmup_key, 0) == 0
    assert rwkv_scheduler._resolved_preset_id_cache[cache_key] == {1: "1000"}
    assert rwkv_scheduler._rwkv_review_input_generation == 1


def test_collection_content_change_invalidates_state_when_preset_changes() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(resolved_preset_id="2000")
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.get_config = lambda _key: {
        "rules": [{"search": "Front:foo", "preset_id": "2000"}]
    }

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            if "from cards" in sql:
                assert args == (10,)
                return [1]
            assert "from revlog" in sql
            assert args == ()
            return [1]

    reviewer.mw.col.db = DB()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )
    cache_key = rwkv_scheduler._preset_id_cache_key(reviewer)
    rwkv_scheduler._resolved_preset_id_cache[cache_key] = {1: "1000"}

    rwkv_scheduler.collection_content_did_change(
        reviewer.mw,
        SimpleNamespace(nid=10, card=SimpleNamespace(id=1)),
    )

    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1
    assert rwkv_scheduler._rwkv_review_input_generation == 1


def test_reviewer_answer_does_not_invalidate_rwkv_queue_caches() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.reviewer = reviewer
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}

    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=reviewer)

    assert rwkv_scheduler._rwkv_study_queue_generation == 0
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {100: {1: 0.25}}
    assert rpc.calls == []


def test_grade_now_reconciles_filtered_answer_and_preserves_resident_state() -> None:
    previous_review_id = (41 * 86_400 + 100) * 1000
    grade_now_review_id = (42 * 86_400 + 100) * 1000
    previous_rows = [
        # The last column is the query's `is_learning_start`.
        (previous_review_id, 1, 10, 100, 3, 500, 1, 4, 2500, 1),
    ]
    grade_now_rows = [
        (grade_now_review_id, 1, 10, 100, 3, 0, 3, 10, 2500),
    ]

    class DB:
        def scalar(self, sql: str, *args: object) -> int:
            assert sql == "select max(id) from revlog"
            assert args == ()
            return previous_review_id

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert "r.cid in (1)" in sql
            if args:
                assert args == (previous_review_id,)
                assert "where r.id > ?" in sql
                return grade_now_rows
            assert "where r.ease between 1 and 4" in sql
            return previous_rows

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    reviewer.mw.col.decks.get_current_id = lambda: 100
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=0, deck_id=999)
    card.odid = 100
    card.current_deck_id = lambda: card.odid or card.did
    reviewer.mw.col.get_card = lambda card_id: card

    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity(
            last_review_id=previous_review_id,
            review_count=1,
        )
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}

    reconciliation = rwkv_scheduler.prepare_grade_now_reconciliation(reviewer, [1])

    assert reconciliation is not None
    assert rwkv_scheduler.record_grade_now_answers(reconciliation) is True
    assert len(runtime.answered_inputs) == 1
    review_input = runtime.answered_inputs[0]
    assert review_input.identity.deck_id == 100
    assert review_input.card_type == int(RwkvReviewState.FILTERED)
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 86_400
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is None

    rwkv_scheduler.study_queues_did_change(
        reviewer.mw,
        initiator=None,
        changes=collection_pb2.OpChanges(card=True, study_queues=True),
    )

    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rwkv_scheduler._rwkv_study_queue_generation == 1
    assert not getattr(
        reviewer,
        rwkv_scheduler._RWKV_GRADE_NOW_RECONCILED_QUEUE_CHANGE_PENDING_ATTR,
    )


def test_grade_now_falls_back_when_learning_replaces_retained_history() -> None:
    previous_review_id = (41 * 86_400 + 100) * 1000
    grade_now_review_id = (42 * 86_400 + 100) * 1000

    class DB:
        def scalar(self, sql: str, *args: object) -> int:
            assert sql == "select max(id) from revlog"
            assert args == ()
            return previous_review_id

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            if args:
                return [(grade_now_review_id, 1, 10, 100, 3, 0, 0, -60, 2500)]
            return [(previous_review_id, 1, 10, 100, 3, 500, 1, 4, 2500, 1)]

    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    reviewer.mw.col.get_card = lambda card_id: _rwkv_card(
        card_id=card_id,
        note_id=10,
        duration_millis=0,
    )
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity(
            last_review_id=previous_review_id,
            review_count=1,
        )
    )

    reconciliation = rwkv_scheduler.prepare_grade_now_reconciliation(reviewer, [1])

    assert reconciliation is not None
    assert rwkv_scheduler.record_grade_now_answers(reconciliation) is False
    assert runtime.answered_inputs == []
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert not getattr(
        reviewer,
        rwkv_scheduler._RWKV_GRADE_NOW_RECONCILED_QUEUE_CHANGE_PENDING_ATTR,
        False,
    )


@pytest.mark.parametrize("changed_deck", [False, True])
def test_collection_mutation_reconciliation_checks_historical_routing(
    monkeypatch: pytest.MonkeyPatch,
    changed_deck: bool,
) -> None:
    current_deck = 100

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "select distinct cid" in sql
            assert args == ()
            return [1]

        def all(self, sql: str, *args: object) -> list[tuple[int, int, int]]:
            assert "select id, nid" in sql
            assert args == ()
            return [(1, 10, current_deck)]

        def scalar(self, sql: str, *args: object) -> int:
            assert sql == "select mod from col"
            assert args == ()
            return 123

    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    reviewer._rwkv_resolved_preset_id = "preset"
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolved_fsrs_preset_ids",
        lambda _reviewer, card_ids: {card_id: "preset" for card_id in card_ids},
    )
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    reconciliation = rwkv_scheduler.prepare_collection_mutation_reconciliation(
        reviewer,
        [1],
    )
    assert reconciliation is not None
    if changed_deck:
        current_deck = 200

    reconciled = rwkv_scheduler.record_collection_mutation_reconciliation(
        reconciliation
    )

    assert reconciled is not changed_deck
    if changed_deck:
        assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    else:
        rwkv_scheduler.study_queues_did_change(
            reviewer.mw,
            initiator=None,
            changes=collection_pb2.OpChanges(card=True, study_queues=True),
        )
        assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states


def test_collection_mutation_wrapper_preserves_metadata_only_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = SimpleNamespace(
        scalar=lambda sql: 123 if sql == "select mod from col" else None
    )
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )
    monkeypatch.setattr("aqt.mw", reviewer.mw)

    changes = rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
        reviewer.mw.col,
        lambda: collection_pb2.OpChanges(config=True, study_queues=True),
    )
    rwkv_scheduler.study_queues_did_change(
        reviewer.mw,
        initiator=None,
        changes=changes,
    )

    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._rwkv_study_queue_generation == 1


def test_new_card_mutation_does_not_wait_for_background_prediction() -> None:
    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "select distinct cid" in sql
            assert args == ()
            return []

        def scalar(self, sql: str, *args: object) -> int:
            assert sql == "select mod from col"
            assert args == ()
            return 123

    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    reconciliation = rwkv_scheduler.prepare_collection_mutation_reconciliation(
        reviewer,
    )
    assert reconciliation is not None

    result: Future[bool] = Future()
    completed = threading.Event()

    def reconcile() -> None:
        try:
            result.set_result(
                rwkv_scheduler.record_collection_mutation_reconciliation(reconciliation)
            )
        except BaseException as exc:
            result.set_exception(exc)
        finally:
            completed.set()

    rwkv_scheduler._reviewer_backend_execution_lock.acquire()
    worker = threading.Thread(target=reconcile)
    try:
        worker.start()
        completed_while_prediction_busy = completed.wait(timeout=1)
    finally:
        rwkv_scheduler._reviewer_backend_execution_lock.release()
        worker.join(timeout=5)

    assert completed_while_prediction_busy
    assert not worker.is_alive()
    assert result.result(timeout=5) is True


def test_collection_mutation_wrapper_preserves_non_queue_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = SimpleNamespace(
        scalar=lambda sql: 123 if sql == "select mod from col" else None
    )
    reviewer.mw.col.get_config = lambda _key: None
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    refreshed_markers: list[object] = []
    monkeypatch.setattr("aqt.mw", reviewer.mw)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_refresh_rwkv_state_cache_collection_mod",
        lambda _reviewer, identity: refreshed_markers.append(identity),
    )

    rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
        reviewer.mw.col,
        lambda: collection_pb2.OpChanges(config=True),
    )
    rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._rwkv_review_input_generation == 1
    assert refreshed_markers == [resident_identity]


def test_collection_content_change_refreshes_preserved_cache_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.get_config = lambda _key: None
    reviewer.mw.col.db = SimpleNamespace()
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    refreshed_markers: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_refresh_rwkv_state_cache_collection_mod",
        lambda _reviewer, identity: refreshed_markers.append(identity),
    )

    rwkv_scheduler.collection_content_did_change(reviewer.mw, initiator=object())

    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert refreshed_markers == [resident_identity]


def test_reconciled_collection_mutation_survives_undo_and_redo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "select distinct cid" in sql
            assert args == ()
            return [1]

        def all(self, sql: str, *args: object) -> list[tuple[int, int, int]]:
            assert "select id, nid" in sql
            assert args == ()
            return [(1, 10, 100)]

        def scalar(self, sql: str, *args: object) -> int:
            assert sql == "select mod from col"
            assert args == ()
            return 123

    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    reviewer.mw.col.get_config = lambda _key: None
    counter = _UndoCounter(reviewer)
    counter.set(4)
    monkeypatch.setattr("aqt.mw", reviewer.mw)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolved_fsrs_preset_ids",
        lambda _reviewer, card_ids: {card_id: "preset" for card_id in card_ids},
    )
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    changes = collection_pb2.OpChanges(card=True, study_queues=True)
    rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
        reviewer.mw.col,
        lambda: changes,
        card_ids=[1],
    )
    assert rwkv_scheduler._rwkv_collection_mutation_undo_entries == []

    def mutate() -> collection_pb2.OpChanges:
        counter.set(5)
        return changes

    rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
        reviewer.mw.col,
        mutate,
        card_ids=[1],
    )
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)

    assert record_collection_undo(_undo_result(counter=5, next_counter=6)) == []
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states

    assert record_collection_redo(_undo_result(counter=6, next_counter=7)) == []
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations.get(warmup_key, 0) == 0


def test_live_learning_restart_requires_canonical_recovery() -> None:
    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert "r.cid in (1)" in sql
            assert args == ()
            return [
                (1_000, 1, 10, 100, 3, 100, 1, 4, 2500),
                (2_000, 1, 10, 100, 3, 100, 0, 1, 2500),
            ]

    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = DB()
    reviewer.mw.col.get_config = lambda _key: {}
    review_input = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        is_query=False,
        ease=3,
        card_type=int(RwkvReviewState.LEARN_START),
    )
    setattr(
        reviewer,
        rwkv_scheduler._REVIEWER_PENDING_ANSWER_STATE_ATTR,
        rwkv_scheduler._RwkvPendingAnswerState(
            1,
            3,
            int(RwkvReviewState.LEARN_START),
            int(RwkvReviewState.LEARN_START),
            2_000,
            review_input,
        ),
    )

    assert (
        rwkv_scheduler._rwkv_live_answer_canonical_recovery_reason(
            reviewer,
            _rwkv_card(card_id=1, note_id=10, duration_millis=0),
            3,
        )
        == "review answer replaced retained learning history"
    )


def test_live_answer_rechecks_dynamic_preset_routing() -> None:
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.get_config = lambda _key: {"rules": [{}]}
    reviewer.mw.col.get_card = lambda _card_id: _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=0,
    )
    reviewer.mw.col.fsrs_preset_for_card = lambda _card_id: SimpleNamespace(
        id="new-preset"
    )
    review_input = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        identity=RwkvReviewIdentity(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id=_expected_preset_hash("old-preset"),
        ),
        is_query=False,
        ease=3,
    )
    setattr(
        reviewer,
        rwkv_scheduler._REVIEWER_PENDING_ANSWER_STATE_ATTR,
        rwkv_scheduler._RwkvPendingAnswerState(
            1,
            3,
            int(RwkvReviewState.REVIEW),
            int(RwkvReviewState.REVIEW),
            2_000,
            review_input,
        ),
    )

    assert (
        rwkv_scheduler._rwkv_live_answer_canonical_recovery_reason(
            reviewer,
            _rwkv_card(card_id=1, note_id=10, duration_millis=0),
            3,
        )
        == "review answer changed RWKV identity routing"
    )


def test_grade_now_batch_can_be_undone_and_redone_incrementally() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    counter.set(7)
    inputs = [
        replace(
            _rwkv_review_input(card_id=1, note_id=10),
            is_query=False,
            ease=3,
        ),
        replace(
            _rwkv_review_input(card_id=2, note_id=20),
            is_query=False,
            ease=4,
        ),
    ]
    query_card = _rwkv_card(card_id=3, note_id=30, duration_millis=0)

    before = backend.predict_review(reviewer=reviewer, card=query_card)
    backend.review_inputs_answered(reviewer, inputs)
    after = backend.predict_review(reviewer=reviewer, card=query_card)
    undone = backend.answer_undone(7, 8)
    after_undo = backend.predict_review(reviewer=reviewer, card=query_card)
    redone = backend.answer_redone(8, 9)
    after_redo = backend.predict_review(reviewer=reviewer, card=query_card)

    assert before is not None
    assert after is not None
    assert after_undo is not None
    assert after_redo is not None
    assert after.retrievability == pytest.approx(0.65)
    assert undone == [1, 2]
    assert after_undo.retrievability == before.retrievability
    assert redone == [1, 2]
    assert after_redo.retrievability == after.retrievability


def test_grade_now_excluded_batch_preserves_undo_and_redo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_review_id = 1_000
    excluded_review_id = 2_000
    # The last column is the query's `is_learning_start`.
    previous_row = (previous_review_id, 1, 10, 100, 3, 500, 1, 4, 2500, 1)
    excluded_row = (excluded_review_id, 1, 10, 100, 3, 0, 3, 4, 0)

    class DB:
        def scalar(self, sql: str, *args: object) -> int:
            assert args == ()
            if sql == "select max(id) from revlog":
                return previous_review_id
            assert sql == "select mod from col"
            return 123

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            if "select id, nid" in sql:
                assert args == ()
                return [(1, 10, 100)]
            if "r.id > ?" in sql:
                assert args == (previous_review_id,)
                return [excluded_row]
            assert "from revlog r" in sql
            assert args == ()
            return [previous_row]

        def list(self, sql: str, *args: object) -> list[int]:
            assert "select distinct cid" in sql
            assert args == ()
            return [1]

    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = DB()
    reviewer.mw.col.get_config = lambda _key: None
    reviewer.mw.col.get_card = lambda _card_id: _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=0,
        deck_id=100,
    )
    counter = _UndoCounter(reviewer)
    counter.set(6)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolved_fsrs_preset_ids",
        lambda _reviewer, card_ids: {card_id: "preset" for card_id in card_ids},
    )
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity(last_review_id=previous_review_id, review_count=1)
    )

    reconciliation = rwkv_scheduler.prepare_grade_now_reconciliation(reviewer, [1])
    assert reconciliation is not None
    counter.set(7)
    assert rwkv_scheduler.record_grade_now_answers(reconciliation)
    assert runtime.answered_inputs == []

    changes = collection_pb2.OpChanges(card=True, study_queues=True)
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)
    assert record_collection_undo(_undo_result(counter=7, next_counter=8)) == []
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states

    assert record_collection_redo(_undo_result(counter=8, next_counter=9)) == []
    rwkv_scheduler.study_queues_did_change(reviewer.mw, None, changes)
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states


def test_reviewer_undo_skips_its_queue_invalidation_once() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = SimpleNamespace()
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [1])
    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=None)

    assert rwkv_scheduler._rwkv_study_queue_generation == 0
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] == (
        resident_identity
    )
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] == (
        0,
        resident_identity,
    )
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {100: {1: 0.25}}
    assert rpc.calls == []

    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=None)

    assert rwkv_scheduler._rwkv_study_queue_generation == 1
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rpc.calls[-1]["scores"] == []


def test_reviewer_redo_skips_generic_invalidation_and_updates_session() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.reviewer = reviewer
    reviewer.mw.col.db = SimpleNamespace()
    reviewer._answeredIds = [1]
    setattr(reviewer, rwkv_scheduler._RWKV_REVIEW_UNDO_CARD_IDS_ATTR, [3, 2])
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}

    rwkv_scheduler.apply_reviewer_redo_card_ids(reviewer, [2])
    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=None)

    assert reviewer._answeredIds == [1, 2]
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 3
    assert not rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler._rwkv_study_queue_generation == 0
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] == (
        resident_identity
    )
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] == (
        0,
        resident_identity,
    )
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {100: {1: 0.25}}
    assert rpc.card_info_calls[-1] == {"card_id": 2, "retrievability": None}


def test_async_reviewer_queue_result_rejects_changed_queue_context() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.decks.deck_and_child_ids = lambda deck_id: [deck_id]
    context = rwkv_scheduler._rwkv_review_queue_context(reviewer, 100)
    assert context is not None
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=context,
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )

    reviewer.mw.col.sched._timing_today = lambda: SimpleNamespace(
        now=43 * 86_400 + 100,
        days_elapsed=43,
        next_day_at=44 * 86_400,
    )

    assert not rwkv_scheduler.install_reviewer_queue_order_async_result(
        reviewer,
        result,
    )
    assert rpc.calls == []


def test_waiting_temporary_operation_does_not_hide_resident_state() -> None:
    backend = SimpleNamespace()
    set_reviewer_backend(cast(Any, backend))
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = object()
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[key] = identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[key] = (0, identity)

    release = threading.Event()
    holder = _start_multi_batch_execution_lock_holder(release)
    entered = threading.Event()
    completed = threading.Event()

    def run_temporary_operation() -> None:
        with rwkv_scheduler._temporary_reviewer_backend_operation(
            reviewer,
            cast(Any, backend),
            cache_snapshot=lambda: "snapshot",
            restore_cache_snapshot=lambda _snapshot: None,
        ) as temporary:
            assert temporary is not None
            entered.set()
        completed.set()

    worker = threading.Thread(target=run_temporary_operation)
    worker.start()
    try:
        assert not entered.wait(timeout=0.1)
        assert rwkv_scheduler._reviewer_backend_warmup_states[key] == identity
        assert rwkv_scheduler._rwkv_memorised_history_identity_cache[key] == (
            0,
            identity,
        )
        assert key not in rwkv_scheduler._reviewer_backend_warmup_pending_generations
    finally:
        release.set()
        holder.join(timeout=5)
        worker.join(timeout=5)

    assert completed.is_set()
    assert rwkv_scheduler._reviewer_backend_warmup_states[key] == identity
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[key] == (0, identity)
    assert key not in rwkv_scheduler._reviewer_backend_warmup_pending_generations


def test_rwkv_memorised_history_builds_progressive_daily_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import rwkv_srs_benchmark

    review_one = replace(
        _rwkv_review_input(card_id=1, note_id=101),
        is_query=False,
        ease=3,
        duration_millis=1000,
        day_offset=10,
        current_elapsed_days=-1,
        current_elapsed_seconds=-1,
    )
    review_two = replace(
        _rwkv_review_input(card_id=2, note_id=102),
        is_query=False,
        ease=3,
        duration_millis=1000,
        day_offset=11,
        current_elapsed_days=-1,
        current_elapsed_seconds=-1,
    )
    history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[review_one, review_two],
        review_ids=[1000, 2000],
        previous_review_id_by_card={1: 1000, 2: 2000},
        previous_interval_days_by_card={1: 4, 2: 4},
        review_count_by_card={1: 1, 2: 1},
        last_review_id=2000,
        review_count=2,
    )

    class Runtime:
        warmups: list[list[RwkvReviewInput]] = []

        def __init__(self, **_kwargs: object) -> None:
            pass

        def warm_up_reviews_in_place(self, reviews: Sequence[RwkvReviewInput]) -> None:
            self.warmups.append(list(reviews))

        def predict_retrievability_many_from_warm_up(
            self, reviews: Sequence[RwkvReviewInput]
        ) -> list[float]:
            return [
                1.0 - 0.1 * (review.current_elapsed_days or 0) for review in reviews
            ]

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", Runtime)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda _reviewer: SimpleNamespace(days_elapsed=11, next_day_at=1_000_000),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_current_embedded_rwkv_model_path",
        lambda: Path("model.bin"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda *_args, **_kwargs: "identity",
    )
    job = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
    )

    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), job)

    assert job.current == 3
    assert job.total == 3
    assert job.completed_through_day == 11
    assert job.retrievability_by_day == pytest.approx([1.0, 1.9])
    assert job.note_retrievability_by_day == pytest.approx([1.0, 1.9])
    assert job.card_count_by_day == [1, 2]
    assert job.result is not None
    assert job.result.identity == "identity"
    assert [(card.card_id, card.start_day) for card in job.result.cards] == [
        (1, 10),
        (2, 11),
    ]
    assert [
        int.from_bytes(job.result.cards[0].values[offset : offset + 2], "little")
        for offset in (0, 2)
    ] == [65_535, round(0.9 * 65_535)]


def test_rwkv_memorised_day_prefers_packed_runtime() -> None:
    import struct

    previous = replace(
        _rwkv_review_input(card_id=1, note_id=101),
        is_query=False,
        ease=3,
        duration_millis=1_000,
        day_offset=10,
    )

    class Runtime:
        def predict_memorised_retrievability_from_warm_up(
            self,
            inputs: Sequence[RwkvReviewInput],
            *,
            day: int,
        ) -> bytes:
            assert list(inputs) == [previous]
            assert day == 12
            return struct.pack("<f", 0.75)

        def predict_retrievability_many_from_warm_up(
            self,
            _inputs: object,
        ) -> list[float]:
            raise AssertionError("tuple resident path should not be used")

    predictions = rwkv_scheduler._predict_rwkv_memorised_day(
        Runtime(),
        [previous],
        day=12,
    )

    assert list(predictions) == pytest.approx([0.75])


def test_rwkv_memorised_start_reuses_identical_active_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = "identity-one"
    started_threads: list[object] = []

    class Thread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            started_threads.append(self)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_memorised_history_job", None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "rwkv_memorised_history_identity",
        lambda _mw: identity,
    )
    monkeypatch.setattr(rwkv_scheduler.threading, "Thread", Thread)

    mw = SimpleNamespace()
    rwkv_scheduler.start_rwkv_memorised_history(mw, [2, 1])
    first_job = rwkv_scheduler._rwkv_memorised_history_job
    assert first_job is not None
    assert len(started_threads) == 1

    rwkv_scheduler.start_rwkv_memorised_history(mw, [1, 2])
    assert rwkv_scheduler._rwkv_memorised_history_job is first_job
    assert len(started_threads) == 1
    assert not first_job.cancel_event.is_set()

    identity = "identity-two"
    rwkv_scheduler.start_rwkv_memorised_history(mw, [1, 2])
    second_job = rwkv_scheduler._rwkv_memorised_history_job
    assert second_job is not None
    assert second_job is not first_job
    assert len(started_threads) == 2
    assert first_job.cancel_event.is_set()
    assert second_job.request_identity == "identity-two"


def test_rwkv_memorised_identity_uses_resident_history_without_db_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DB:
        def all(self, _sql: str) -> list[tuple[int, int]]:
            pytest.fail("resident Memorised identity must not query revlog")

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_ready_state_cache_history_identity",
        lambda _reviewer: _rwkv_resident_identity(
            last_review_id=5000,
            review_count=3,
            history_hash=rwkv_scheduler._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH,
            replay_key="replay-key",
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: pytest.fail(
            "ready cache identity should avoid a history scan"
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, **values: json.dumps(
            {
                "lastReviewId": values["last_review_id"],
                "reviewCount": values["review_count"],
                "historyHash": values["history_hash"],
                "replayKey": values["replay_key"],
            }
        ),
    )
    identity = json.loads(
        rwkv_scheduler.rwkv_memorised_history_identity(
            SimpleNamespace(col=SimpleNamespace(db=DB()))
        )
    )

    assert identity == {
        "lastReviewId": 5000,
        "reviewCount": 3,
        "historyHash": rwkv_scheduler._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH,
        "replayKey": "replay-key",
    }


def test_rwkv_memorised_identity_falls_back_to_canonical_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DB:
        def all(self, _sql: str) -> list[tuple[int, int]]:
            pytest.fail("fallback must use the canonical history builder directly")

    history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card={},
        previous_interval_days_by_card={},
        review_count_by_card={},
        last_review_id=2000,
        review_count=2,
        history_hash="a" * 64,
        replay_key="canonical-replay",
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_ready_state_cache_history_identity",
        lambda _reviewer: None,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, **values: json.dumps(values),
    )

    identity = json.loads(
        rwkv_scheduler.rwkv_memorised_history_identity(
            SimpleNamespace(col=SimpleNamespace(db=DB()))
        )
    )

    assert identity["last_review_id"] == 2000
    assert identity["review_count"] == 2
    assert identity["history_hash"] == "a" * 64
    assert identity["replay_key"] == "canonical-replay"


def test_rwkv_memorised_canonical_identity_is_cached_when_runtime_identity_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = None
    history = _rwkv_canonical_history()
    scans = 0

    def canonical_history(
        _reviewer: object,
    ) -> rwkv_scheduler.RwkvHistoricalReviewInputs:
        nonlocal scans
        scans += 1
        return history

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        canonical_history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, **values: cast(str, values["history_hash"]),
    )

    assert rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw) == "d" * 64
    assert rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw) == "d" * 64

    assert scans == 1
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] == (
        0,
        rwkv_scheduler._resident_state_identity(history),
    )


def test_rwkv_memorised_canonical_scan_crossing_mutation_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = None
    history = _rwkv_canonical_history()
    scans = 0
    scan_started = threading.Event()
    resume_scan = threading.Event()

    def canonical_history(
        _reviewer: object,
    ) -> rwkv_scheduler.RwkvHistoricalReviewInputs:
        nonlocal scans
        scans += 1
        if scans == 1:
            scan_started.set()
            assert resume_scan.wait(timeout=5)
        return history

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        canonical_history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, **values: cast(str, values["history_hash"]),
    )

    identities: list[str] = []
    scan_thread = threading.Thread(
        target=lambda: identities.append(
            rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw)
        )
    )
    scan_thread.start()
    assert scan_started.wait(timeout=5)
    rwkv_scheduler._invalidate_reviewer_backend_state(
        reviewer,
        reason="test mutation during canonical scan",
    )
    resume_scan.set()
    scan_thread.join(timeout=5)

    assert not scan_thread.is_alive()
    assert identities == ["d" * 64]
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache

    assert rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw) == "d" * 64
    assert scans == 2
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key][0] == 1


def test_rwkv_memorised_canonical_identity_is_invalidated_by_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = None
    history = _rwkv_canonical_history()
    scans = 0

    def canonical_history(
        _reviewer: object,
    ) -> rwkv_scheduler.RwkvHistoricalReviewInputs:
        nonlocal scans
        scans += 1
        return history

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        canonical_history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, **values: cast(str, values["history_hash"]),
    )

    rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw)
    assert warmup_key in rwkv_scheduler._rwkv_memorised_history_identity_cache

    rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    rwkv_scheduler.rwkv_memorised_history_identity(reviewer.mw)
    assert scans == 2


def test_rwkv_memorised_identity_includes_canonical_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_cache_key",
        lambda _reviewer: {"collection": "test"},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda _reviewer: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_first_review_elapsed_config_key",
        lambda _reviewer: [],
    )
    monkeypatch.setattr(rwkv_scheduler, "_day_offset", lambda _reviewer: 42)

    identity = json.loads(
        rwkv_scheduler._rwkv_memorised_history_identity(
            SimpleNamespace(),
            last_review_id=2000,
            review_count=2,
            history_hash="b" * 64,
            replay_key="canonical-replay",
        )
    )

    assert identity["version"] == 3
    assert identity["historyHash"] == "b" * 64
    assert identity["replayKey"] == "canonical-replay"


def test_rwkv_memorised_identity_uses_only_resident_state_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = SimpleNamespace()
    warmup_key = (1, 2)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_key",
        lambda _reviewer: warmup_key,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_rwkv_state_cache_metadata",
        lambda _reviewer: pytest.fail(
            "Memorised must not infer resident identity from disk"
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda _reviewer, **_kwargs: pytest.fail(
            "resident identity must not recompute replay semantics"
        ),
    )
    assert rwkv_scheduler._rwkv_ready_state_cache_history_identity(reviewer) is None

    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity

    assert (
        rwkv_scheduler._rwkv_ready_state_cache_history_identity(reviewer)
        is resident_identity
    )


def test_rwkv_memorised_unknown_resident_identity_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = SimpleNamespace()
    warmup_key = (1, 2)
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = None
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_key",
        lambda _reviewer: warmup_key,
    )

    assert rwkv_scheduler._rwkv_ready_state_cache_history_identity(reviewer) is None
    assert warmup_key in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is None


def test_cache_restore_does_not_publish_after_collection_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None

    def restore(
        _reviewer: object,
        **_kwargs: object,
    ) -> rwkv_scheduler.RwkvResidentStateIdentity:
        rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)
        return _rwkv_resident_identity()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_pending_generations


def test_unavailable_cache_restore_is_not_retried_for_same_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    restore_calls = 0

    def restore(_reviewer: object, **_kwargs: object) -> None:
        nonlocal restore_calls
        restore_calls += 1

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert restore_calls == 1

    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    assert rwkv_scheduler._reviewer_backend_cold_fallback_generations[warmup_key] == 0

    rwkv_scheduler._invalidate_reviewer_backend_state(
        reviewer,
        reason="unrelated collection mutation",
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert restore_calls == 2


def test_failed_cache_restore_is_not_retried_for_same_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    restore_calls = 0

    def restore(_reviewer: object, **_kwargs: object) -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise RuntimeError("cache read failed")

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert restore_calls == 1

    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    assert rwkv_scheduler._reviewer_backend_cold_fallback_generations[warmup_key] == 0


def test_cold_answer_does_not_retry_unavailable_cache_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.col.db = SimpleNamespace()
    restore_calls = 0

    def restore(_reviewer: object, **_kwargs: object) -> None:
        nonlocal restore_calls
        restore_calls += 1

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    record_reviewer_answer(
        reviewer,
        _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        ease=3,
    )
    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert restore_calls == 1

    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1
    assert rwkv_scheduler._reviewer_backend_cold_fallback_generations[warmup_key] == 1


def test_deleted_reviewer_card_defers_cache_restore_for_current_generation() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()

    rwkv_scheduler.defer_reviewer_backend_cache_restore(
        reviewer,
        reason="reviewer card deleted",
    )

    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    assert rwkv_scheduler._reviewer_backend_cold_fallback_generations[warmup_key] == 0
    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False


def test_cache_publish_and_invalidation_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None

    publish_write_started = threading.Event()
    allow_publish_write = threading.Event()

    class BlockingWarmupStates(
        dict[
            tuple[int, int],
            rwkv_scheduler.RwkvResidentStateIdentity | None,
        ]
    ):
        def __setitem__(
            self,
            key: tuple[int, int],
            value: rwkv_scheduler.RwkvResidentStateIdentity | None,
        ) -> None:
            publish_write_started.set()
            assert allow_publish_write.wait(timeout=5)
            super().__setitem__(key, value)

    states = BlockingWarmupStates()
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_states",
        states,
    )
    publish_result: Future[bool] = Future()
    invalidation_result: Future[None] = Future()

    def publish() -> None:
        try:
            publish_result.set_result(
                rwkv_scheduler._publish_reviewer_backend_state(
                    warmup_key,
                    _rwkv_resident_identity(),
                    expected_generation=0,
                )
            )
        except BaseException as exc:
            publish_result.set_exception(exc)

    def invalidate() -> None:
        try:
            rwkv_scheduler._invalidate_reviewer_backend_state(
                reviewer,
                reason="concurrent mutation",
            )
            invalidation_result.set_result(None)
        except BaseException as exc:
            invalidation_result.set_exception(exc)

    publish_thread = threading.Thread(target=publish)
    invalidate_thread = threading.Thread(target=invalidate)
    publish_thread.start()
    assert publish_write_started.wait(timeout=5)
    invalidate_thread.start()
    try:
        assert not invalidation_result.done()
    finally:
        allow_publish_write.set()
    publish_thread.join(timeout=5)
    invalidate_thread.join(timeout=5)

    assert not publish_thread.is_alive()
    assert not invalidate_thread.is_alive()
    assert publish_result.result(timeout=5) is True
    assert invalidation_result.result(timeout=5) is None
    assert warmup_key not in states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1


def test_concurrent_cold_cache_prepare_starts_one_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    restore_started = threading.Event()
    release_restore = threading.Event()
    start = threading.Barrier(3)
    restore_calls = 0
    restore_calls_lock = threading.Lock()

    def restore(
        _reviewer: object,
        **_kwargs: object,
    ) -> rwkv_scheduler.RwkvResidentStateIdentity:
        nonlocal restore_calls
        with restore_calls_lock:
            restore_calls += 1
        restore_started.set()
        assert release_restore.wait(timeout=5)
        return _rwkv_resident_identity()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )
    results = [Future[bool](), Future[bool]()]

    def prepare(result: Future[bool]) -> None:
        try:
            start.wait(timeout=5)
            result.set_result(
                rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer)
            )
        except BaseException as exc:
            result.set_exception(exc)

    threads = [threading.Thread(target=prepare, args=(result,)) for result in results]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    assert restore_started.wait(timeout=5)
    release_restore.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert restore_calls == 1
    assert any(result.result(timeout=5) for result in results)


def test_cache_prepare_clears_pending_before_releasing_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    pending_cleared = threading.Event()
    release_finish = threading.Event()
    original_finish = rwkv_scheduler._finish_reviewer_backend_warmup

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        lambda *_args, **_kwargs: identity,
    )

    def finish(warmup_key: tuple[int, int], generation: int) -> None:
        original_finish(warmup_key, generation)
        pending_cleared.set()
        assert release_finish.wait(timeout=5)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_finish_reviewer_backend_warmup",
        finish,
    )

    prepare_result: Future[bool] = Future()

    def prepare() -> None:
        prepare_result.set_result(
            rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer)
        )

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert pending_cleared.wait(timeout=5)
    assert rwkv_scheduler._reviewer_backend_warmup_states[key] == identity
    assert key not in rwkv_scheduler._reviewer_backend_warmup_pending_generations

    answer_completed = threading.Event()

    def answer() -> None:
        record_reviewer_answer(
            reviewer,
            _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
            ease=3,
        )
        answer_completed.set()

    answer_thread = threading.Thread(target=answer)
    answer_thread.start()
    try:
        assert not answer_completed.wait(timeout=0.1)
        assert rwkv_scheduler._reviewer_backend_warmup_states[key] == identity
    finally:
        release_finish.set()
        prepare_thread.join(timeout=5)
        answer_thread.join(timeout=5)

    assert prepare_result.result(timeout=5) is True
    assert answer_completed.is_set()
    assert runtime.reviewed == [(1, 3)]


def test_force_rebuild_invalidates_an_in_flight_warmup() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    identity = _rwkv_resident_identity()
    with rwkv_scheduler._reviewer_backend_state_lock:
        rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = identity
        rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] = 4
        rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] = 4

    assert (
        rwkv_scheduler._warm_up_reviewer_backend(
            reviewer,
            force_rebuild=True,
        )
        is False
    )

    with rwkv_scheduler._reviewer_backend_state_lock:
        assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
        assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 5
        assert (
            rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] == 4
        )
    assert (
        rwkv_scheduler._publish_reviewer_backend_state(
            warmup_key,
            identity,
            expected_generation=4,
        )
        is False
    )


def test_published_state_is_not_ready_until_execution_pending_finishes() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    identity = _rwkv_resident_identity()
    with rwkv_scheduler._reviewer_backend_state_lock:
        rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = identity
        rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] = 3
        rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] = 3

    assert rwkv_scheduler._reviewer_backend_warmed_up(reviewer) is False
    assert rwkv_scheduler._rwkv_ready_state_cache_history_identity(reviewer) is None

    rwkv_scheduler._finish_reviewer_backend_warmup(warmup_key, 3)

    assert rwkv_scheduler._reviewer_backend_warmed_up(reviewer) is True
    assert rwkv_scheduler._rwkv_ready_state_cache_history_identity(reviewer) is identity


def test_profile_open_reset_preserves_in_flight_warmup_tombstone() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._rwkv_startup_build_started = True
    with rwkv_scheduler._reviewer_backend_state_lock:
        rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] = 7
        rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] = 7

    rwkv_scheduler._invalidate_reviewer_backend_runtime_state_for_profile_open()

    with rwkv_scheduler._reviewer_backend_state_lock:
        assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 8
        assert (
            warmup_key
            not in rwkv_scheduler._reviewer_backend_warmup_pending_generations
        )
        rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] = 8
    rwkv_scheduler._finish_reviewer_backend_warmup(warmup_key, 7)
    with rwkv_scheduler._reviewer_backend_state_lock:
        assert (
            rwkv_scheduler._reviewer_backend_warmup_pending_generations[warmup_key] == 8
        )
    assert rwkv_scheduler._rwkv_startup_build_started is False


def test_cache_prepare_uses_the_backend_captured_at_begin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    replacement_backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(original_backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    captured_backends: list[object] = []

    def restore(
        _reviewer: object,
        *,
        backend: object,
        **_kwargs: object,
    ) -> rwkv_scheduler.RwkvResidentStateIdentity:
        captured_backends.append(backend)
        set_reviewer_backend(replacement_backend)
        return _rwkv_resident_identity()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert captured_backends == [original_backend]
    assert rwkv_scheduler._reviewer_backend is replacement_backend
    assert rwkv_scheduler._reviewer_backend_warmup_states == {}


def test_profile_warmups_serialize_backend_runtime_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    old_reviewer = _rwkv_reviewer()
    new_reviewer = _rwkv_reviewer()
    old_reviewer.mw.col.db = SimpleNamespace()
    new_reviewer.mw.col.db = SimpleNamespace()
    old_restore_started = threading.Event()
    release_old_restore = threading.Event()
    new_prepare_started = threading.Event()
    restore_order: list[str] = []
    runtime_profile: list[str] = []

    def restore(
        reviewer: object,
        *,
        backend: object,
        **_kwargs: object,
    ) -> rwkv_scheduler.RwkvResidentStateIdentity:
        assert backend is rwkv_scheduler._reviewer_backend
        profile = "old" if reviewer is old_reviewer else "new"
        if profile == "old":
            old_restore_started.set()
            assert release_old_restore.wait(timeout=5)
        restore_order.append(profile)
        runtime_profile[:] = [profile]
        return _rwkv_resident_identity()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )
    old_result: Future[bool] = Future()
    new_result: Future[bool] = Future()

    def prepare(reviewer: object, result: Future[bool]) -> None:
        try:
            if reviewer is new_reviewer:
                new_prepare_started.set()
            result.set_result(
                rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer)
            )
        except BaseException as exc:
            result.set_exception(exc)

    old_thread = threading.Thread(
        target=prepare,
        args=(old_reviewer, old_result),
    )
    new_thread = threading.Thread(
        target=prepare,
        args=(new_reviewer, new_result),
    )
    old_thread.start()
    assert old_restore_started.wait(timeout=5)
    rwkv_scheduler._invalidate_reviewer_backend_runtime_state_for_profile_open()
    new_thread.start()
    restore_order_before_release: list[str] = []
    try:
        assert new_prepare_started.wait(timeout=5)
        assert not new_result.done()
        restore_order_before_release = list(restore_order)
    finally:
        release_old_restore.set()
    old_thread.join(timeout=5)
    new_thread.join(timeout=5)

    assert restore_order_before_release == []
    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert old_result.result(timeout=5) is False
    assert new_result.result(timeout=5) is True
    assert restore_order == ["old", "new"]
    assert runtime_profile == ["new"]


def test_queued_cache_prepare_claims_generation_after_profile_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    prepare_started = threading.Event()
    restore_calls: list[object] = []

    def restore(*args: object, **kwargs: object) -> object:
        restore_calls.append((args, kwargs))
        return _rwkv_resident_identity()

    monkeypatch.setattr(rwkv_scheduler, "_restore_reviewer_backend_cache", restore)
    result: Future[bool] = Future()

    def prepare() -> None:
        try:
            prepare_started.set()
            result.set_result(
                rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer)
            )
        except BaseException as exc:
            result.set_exception(exc)

    rwkv_scheduler._reviewer_backend_execution_lock.acquire()
    thread = threading.Thread(target=prepare)
    queued = False
    try:
        thread.start()
        queued = prepare_started.wait(timeout=5)
        assert not result.done()
        assert not rwkv_scheduler._reviewer_backend_warmup_pending(reviewer)
        rwkv_scheduler._invalidate_reviewer_backend_runtime_state_for_profile_open()
    finally:
        rwkv_scheduler._reviewer_backend_execution_lock.release()
    thread.join(timeout=5)

    assert queued
    assert not thread.is_alive()
    assert result.result(timeout=5) is True
    assert len(restore_calls) == 1


def test_rwkv_memorised_cancel_checkpoint_resumes_without_repredicting_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import rwkv_srs_benchmark

    reviews = [
        replace(
            _rwkv_review_input(card_id=1, note_id=101),
            is_query=False,
            ease=3,
            duration_millis=1000,
            day_offset=10,
        ),
        replace(
            _rwkv_review_input(card_id=2, note_id=102),
            is_query=False,
            ease=3,
            duration_millis=1000,
            day_offset=11,
        ),
    ]
    history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=[1000, 2000],
        previous_review_id_by_card={1: 1000, 2: 2000},
        previous_interval_days_by_card={1: 4, 2: 4},
        review_count_by_card={1: 1, 2: 1},
        last_review_id=2000,
        review_count=2,
    )
    jobs: list[rwkv_scheduler.RwkvMemorisedHistoryJob] = []

    class Runtime:
        instances: list[Runtime] = []

        def __init__(self, **_kwargs: object) -> None:
            self.warmup_sizes: list[int] = []
            self.prediction_sizes: list[int] = []
            self.instances.append(self)

        def warm_up_reviews_in_place(self, inputs: Sequence[RwkvReviewInput]) -> None:
            self.warmup_sizes.append(len(inputs))

        def predict_retrievability_many_from_warm_up(
            self, inputs: Sequence[RwkvReviewInput]
        ) -> list[float]:
            self.prediction_sizes.append(len(inputs))
            if len(self.instances) == 1:
                jobs[0].cancel_event.set()
            return [0.8] * len(inputs)

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", Runtime)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda _reviewer: SimpleNamespace(days_elapsed=11, next_day_at=1_000_000),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_current_embedded_rwkv_model_path",
        lambda: Path("model.bin"),
    )
    monkeypatch.setattr(rwkv_scheduler, "_deck_config_for_deck_id", lambda *_args: None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda *_args, **_kwargs: "identity",
    )

    cancelled = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
    )
    jobs.append(cancelled)
    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), cancelled)

    checkpoint = cancelled.result
    assert checkpoint is not None
    assert not checkpoint.complete
    assert checkpoint.completed_through_day == 10
    assert cancelled.phase == "cancelled"

    resumed = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
        checkpoint=checkpoint,
    )
    jobs.append(resumed)
    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), resumed)

    assert resumed.result is not None
    assert resumed.result.complete
    assert resumed.result.completed_through_day == 11
    assert Runtime.instances[1].warmup_sizes == [1, 1]
    assert Runtime.instances[1].prediction_sizes == [2]


def test_rwkv_memorised_completed_cache_reuses_days_before_new_review() -> None:
    reviews = [
        replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10),
        replace(_rwkv_review_input(card_id=2, note_id=102), day_offset=11),
    ]
    cached_identity = _rwkv_memorised_test_identity(
        day_offset=11,
        review_ids=[1000],
        reviews=reviews[:1],
    )
    current_identity = _rwkv_memorised_test_identity(
        day_offset=11,
        review_ids=[1000, 2000],
        reviews=reviews,
    )
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=cached_identity,
        first_day=10,
        last_day=11,
        cards=(
            rwkv_scheduler.RwkvMemorisedCardSeries(
                card_id=1,
                note_id=101,
                start_day=10,
                values=(50_000).to_bytes(2, "little") + (40_000).to_bytes(2, "little"),
            ),
        ),
        completed_through_day=11,
        total=2,
        complete=True,
    )
    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=current_identity,
        first_day=10,
        last_day=11,
        total=3,
        reviews=reviews,
        review_ids=[1000, 2000],
    )

    assert checkpoint is not None
    assert checkpoint.identity == current_identity
    assert checkpoint.completed_through_day == 10
    assert checkpoint.last_day == 11
    assert checkpoint.total == 3
    assert not checkpoint.complete
    assert checkpoint.cards == (
        replace(completed.cards[0], values=(50_000).to_bytes(2, "little")),
    )


def test_rwkv_memorised_completed_cache_rejects_changed_prefix_content() -> None:
    cached_review = replace(
        _rwkv_review_input(card_id=1, note_id=101),
        is_query=False,
        ease=3,
        day_offset=10,
    )
    changed_review = replace(cached_review, ease=1)
    appended_review = replace(
        _rwkv_review_input(card_id=2, note_id=102),
        is_query=False,
        ease=3,
        day_offset=11,
    )
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=_rwkv_memorised_test_identity(
            day_offset=11,
            review_ids=[1000],
            reviews=[cached_review],
        ),
        first_day=10,
        last_day=11,
        cards=(
            rwkv_scheduler.RwkvMemorisedCardSeries(
                card_id=1,
                note_id=101,
                start_day=10,
                values=(50_000).to_bytes(2, "little") + (40_000).to_bytes(2, "little"),
            ),
        ),
        completed_through_day=11,
        total=2,
        complete=True,
    )
    reviews = [changed_review, appended_review]
    current_identity = _rwkv_memorised_test_identity(
        day_offset=11,
        review_ids=[1000, 2000],
        reviews=reviews,
    )

    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=current_identity,
        first_day=10,
        last_day=11,
        total=3,
        reviews=reviews,
        review_ids=[1000, 2000],
    )

    assert checkpoint is None


def test_rwkv_memorised_completed_cache_appends_new_scheduler_day() -> None:
    reviews = [replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10)]
    cached_identity = _rwkv_memorised_test_identity(
        day_offset=11,
        review_ids=[1000],
        reviews=reviews,
    )
    current_identity = _rwkv_memorised_test_identity(
        day_offset=12,
        review_ids=[1000],
        reviews=reviews,
    )
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=cached_identity,
        first_day=10,
        last_day=11,
        cards=(
            rwkv_scheduler.RwkvMemorisedCardSeries(
                card_id=1,
                note_id=101,
                start_day=10,
                values=(50_000).to_bytes(2, "little") + (40_000).to_bytes(2, "little"),
            ),
        ),
        completed_through_day=11,
        total=2,
        complete=True,
    )
    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=current_identity,
        first_day=10,
        last_day=12,
        total=3,
        reviews=reviews,
        review_ids=[1000],
    )

    assert checkpoint is not None
    assert checkpoint.completed_through_day == 11
    assert checkpoint.last_day == 12
    assert checkpoint.cards == completed.cards


def test_rwkv_memorised_completed_cache_rejects_model_change() -> None:
    reviews = [replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10)]
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=_rwkv_memorised_test_identity(
            day_offset=11,
            review_ids=[1000],
            reviews=reviews,
            model="old",
        ),
        first_day=10,
        last_day=11,
        cards=(),
        completed_through_day=11,
        complete=True,
    )

    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=_rwkv_memorised_test_identity(
            day_offset=12,
            review_ids=[1000],
            reviews=reviews,
            model="new",
        ),
        first_day=10,
        last_day=12,
        total=3,
        reviews=reviews,
        review_ids=[1000],
    )

    assert checkpoint is None


def test_rust_rwkv_warm_up_in_place_skips_snapshot_serialization() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        calls: list[tuple[list[tuple[object, ...]], bool]] = []

        def warm_up_reviews(
            self,
            rows: list[tuple[object, ...]],
            record_predictions: bool,
        ) -> list[object]:
            self.calls.append((rows, record_predictions))
            return []

    runtime = object.__new__(_RustRwkvRuntime)
    runtime._process = Process()
    runtime._process_lock = threading.RLock()

    runtime.warm_up_reviews_in_place([_rwkv_review_input(card_id=1, note_id=101)])

    assert len(runtime._process.calls) == 1
    assert runtime._process.calls[0][1] is False


def test_rwkv_queue_refresh_on_exit_uses_nested_config() -> None:
    class Decks:
        def get_current_id(self) -> int:
            return 100

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": False,
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_on_exit": True,
                    }
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))

    assert rwkv_scheduler.reviewer_queue_order_refresh_on_exit_enabled(reviewer)
    assert rwkv_scheduler.reviewer_queue_order_exit_refresh_needed(reviewer)


def test_rwkv_queue_exit_refresh_skips_current_queue_scores() -> None:
    class Decks:
        def get_current_id(self) -> int:
            return 100

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": False,
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_on_exit": True,
                    }
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    previous_backend = set_reviewer_backend(SimpleNamespace(state_generation=lambda: 7))
    try:
        rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.9}
        rwkv_scheduler._rwkv_review_queue_score_generations[100] = 7

        assert not rwkv_scheduler.reviewer_queue_order_exit_refresh_needed(reviewer)
    finally:
        set_reviewer_backend(previous_backend)


def test_rwkv_queue_refresh_due_uses_direct_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "rwkvReviewEnabled": False,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewRefreshInterval": 2,
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())),
        card=SimpleNamespace(id=1, did=100),
        _answeredIds=[1],
    )

    assert not rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)

    reviewer._answeredIds.append(2)

    assert rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)


def test_rwkv_queue_refresh_on_exit_uses_direct_config() -> None:
    class Decks:
        def get_current_id(self) -> int:
            return 100

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "rwkvReviewEnabled": False,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewRefreshOnExit": True,
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))

    assert rwkv_scheduler.reviewer_queue_order_refresh_on_exit_enabled(reviewer)


def test_interval_from_recall_curve_interpolates_target() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=6, retrievability=0.80),
            RwkvRecallPoint(elapsed_days=0, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=2, retrievability=0.90),
        ],
        target_retention=0.86,
        max_interval_days=36500,
    )

    assert interval == 4


def test_interval_from_recall_curve_returns_max_when_target_not_reached() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=0, retrievability=0.98),
            RwkvRecallPoint(elapsed_days=7, retrievability=0.93),
        ],
        target_retention=0.90,
        max_interval_days=365,
    )

    assert interval == 365


def test_interval_from_recall_curve_clamps_to_review_day_bounds() -> None:
    immediate_interval = interval_from_recall_curve(
        [RwkvRecallPoint(elapsed_days=0, retrievability=0.80)],
        target_retention=0.90,
        max_interval_days=36500,
    )
    max_interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=1, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=100, retrievability=0.50),
        ],
        target_retention=0.90,
        max_interval_days=5,
    )

    assert immediate_interval == 1
    assert max_interval == 5


def test_interval_from_recall_curve_returns_none_for_nonmonotonic_curve() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=0, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=2, retrievability=0.90),
            RwkvRecallPoint(elapsed_days=5, retrievability=0.92),
        ],
        target_retention=0.91,
        max_interval_days=36500,
    )

    assert interval is None


@pytest.mark.parametrize(
    "points",
    [
        [RwkvRecallPoint(elapsed_days=math.inf, retrievability=0.90)],
        [RwkvRecallPoint(elapsed_days=-1, retrievability=0.90)],
        [RwkvRecallPoint(elapsed_days=1, retrievability=1.01)],
        [
            RwkvRecallPoint(elapsed_days=1, retrievability=0.90),
            RwkvRecallPoint(elapsed_days=1, retrievability=0.80),
        ],
    ],
)
def test_interval_from_recall_curve_rejects_invalid_points(
    points: list[RwkvRecallPoint],
) -> None:
    with pytest.raises(ValueError):
        interval_from_recall_curve(
            points,
            target_retention=0.90,
            max_interval_days=36500,
        )


@pytest.mark.parametrize("target_retention", [math.nan, -0.01, 1.01])
def test_interval_from_recall_curve_rejects_invalid_target(
    target_retention: float,
) -> None:
    with pytest.raises(ValueError):
        interval_from_recall_curve(
            [RwkvRecallPoint(elapsed_days=1, retrievability=0.90)],
            target_retention=target_retention,
            max_interval_days=36500,
        )


def test_apply_review_interval_overrides_changes_only_answer_review_states() -> None:
    states = SchedulingStates()
    states.current.CopyFrom(_normal_review_state(interval=9, fuzz_delta=7))
    states.again.CopyFrom(_normal_review_state(interval=1, fuzz_delta=1))
    states.hard.CopyFrom(_normal_review_state(interval=2, fuzz_delta=2))
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.easy.CopyFrom(_normal_review_state(interval=4, fuzz_delta=4))

    updated = apply_review_interval_overrides(
        states,
        RwkvIntervalOverride(again=10, hard=20, good=30, easy=40),
        RwkvIntervalOverride(again=11, hard=22, good=33, easy=44),
    )

    assert updated.current.normal.review.scheduled_days == 9
    assert updated.current.normal.review.fuzz_delta_days == 7
    assert updated.again.normal.review.scheduled_days == 10
    assert updated.hard.normal.review.scheduled_days == 20
    assert updated.good.normal.review.scheduled_days == 30
    assert updated.easy.normal.review.scheduled_days == 40
    assert updated.again.normal.review.fuzz_delta_days == 0
    assert updated.hard.normal.review.fuzz_delta_days == 0
    assert updated.good.normal.review.fuzz_delta_days == 0
    assert updated.easy.normal.review.fuzz_delta_days == 0
    assert updated.again.normal.review.memory_state.stability == pytest.approx(11)
    assert updated.hard.normal.review.memory_state.stability == pytest.approx(22)
    assert updated.good.normal.review.memory_state.stability == pytest.approx(33)
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(44)
    assert not updated.good.normal.review.memory_state.HasField("stability_internal")
    assert updated.good.normal.review.memory_state.difficulty == pytest.approx(5.0)

    assert states.again.normal.review.scheduled_days == 1
    assert states.hard.normal.review.scheduled_days == 2
    assert states.good.normal.review.scheduled_days == 3
    assert states.easy.normal.review.scheduled_days == 4


def test_apply_review_interval_overrides_skips_non_review_states() -> None:
    states = SchedulingStates()
    states.again.CopyFrom(_learning_state())
    states.hard.CopyFrom(_relearning_state())
    states.good.CopyFrom(_filtered_preview_state())
    states.easy.CopyFrom(_normal_review_state(interval=4, fuzz_delta=4))

    updated = apply_review_interval_overrides(
        states,
        RwkvIntervalOverride(again=10, hard=20, good=30, easy=40),
        RwkvIntervalOverride(again=11, hard=22, good=33, easy=44),
    )

    assert updated.again.normal.learning.scheduled_secs == 60
    assert updated.hard.normal.relearning.review.scheduled_days == 20
    assert updated.hard.normal.relearning.review.fuzz_delta_days == 0
    assert (
        updated.hard.normal.relearning.review.memory_state.stability
        == pytest.approx(22)
    )
    assert updated.hard.normal.relearning.learning.scheduled_secs == 120
    assert updated.good.filtered.preview.scheduled_secs == 180
    assert updated.easy.normal.review.scheduled_days == 40
    assert updated.easy.normal.review.fuzz_delta_days == 0
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(44)


def test_apply_review_interval_overrides_rejects_invalid_interval() -> None:
    with pytest.raises(ValueError):
        apply_review_interval_overrides(
            SchedulingStates(),
            RwkvIntervalOverride(good=0),
        )


def test_apply_review_interval_overrides_records_fuzz_deltas() -> None:
    states = SchedulingStates()
    states.hard.CopyFrom(_relearning_state())
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.easy.CopyFrom(_normal_review_state(interval=4, fuzz_delta=4))

    updated = apply_review_interval_overrides(
        states,
        RwkvIntervalOverride(hard=20, good=32, easy=41),
        fuzz_deltas=RwkvIntervalOverride(hard=-1, good=2),
    )

    assert updated.hard.normal.relearning.review.scheduled_days == 20
    assert updated.hard.normal.relearning.review.fuzz_delta_days == -1
    assert updated.good.normal.review.scheduled_days == 32
    assert updated.good.normal.review.fuzz_delta_days == 2
    # a rating without a delta is recorded as unfuzzed
    assert updated.easy.normal.review.scheduled_days == 41
    assert updated.easy.normal.review.fuzz_delta_days == 0


def _states_backend(
    requests: list[scheduler_pb2.SchedulingStatesWithIntervalsRequest],
    rebuilt: SchedulingStates,
) -> SimpleNamespace:
    def scheduling_states_with_intervals(
        request: scheduler_pb2.SchedulingStatesWithIntervalsRequest,
    ) -> SchedulingStates:
        requests.append(request)
        return rebuilt

    return SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(
                    scheduling_states_with_intervals=scheduling_states_with_intervals
                )
            )
        )
    )


def test_rwkv_curve_states_come_from_the_backend_with_unrounded_intervals() -> None:
    """Pins spec/scheduling.md#sched.sub-day-intervals and #sched.rwkv-curve-fuzz."""

    requests: list[scheduler_pb2.SchedulingStatesWithIntervalsRequest] = []
    rebuilt = SchedulingStates()
    rebuilt.again.CopyFrom(_relearning_state())
    rebuilt.hard.CopyFrom(_normal_review_state(interval=2, fuzz_delta=0))
    rebuilt.good.CopyFrom(_normal_review_state(interval=9, fuzz_delta=-1))
    rebuilt.easy.CopyFrom(_normal_review_state(interval=20, fuzz_delta=2))
    reviewer = _states_backend(requests, rebuilt)
    card = _rwkv_card(card_id=7, note_id=70, duration_millis=100)
    original = SchedulingStates()
    original.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = rwkv_curve_scheduling_states(
        reviewer,
        card,
        original,
        RwkvIntervalOverride(again=0.2, hard=1.5, good=9.4, easy=18.0),
        RwkvIntervalOverride(again=1, hard=2, good=10, easy=19),
    )

    assert len(requests) == 1
    assert requests[0].card_id == 7
    assert (
        requests[0].again,
        requests[0].hard,
        requests[0].good,
        requests[0].easy,
    ) == pytest.approx((0.2, 1.5, 9.4, 18.0))
    # the S90s go along, for the young-leech check (spec sched.rwkv-curve-fuzz)
    assert (
        requests[0].again_s90,
        requests[0].hard_s90,
        requests[0].good_s90,
        requests[0].easy_s90,
    ) == pytest.approx((1, 2, 10, 19))
    # the backend's states are used, with each button's S90 as its stability
    assert updated.again.normal.relearning.learning.scheduled_secs == 120
    assert updated.good.normal.review.scheduled_days == 9
    assert updated.good.normal.review.fuzz_delta_days == -1
    assert updated.good.normal.review.memory_state.stability == pytest.approx(10)
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(19)
    # the input states are left alone
    assert original.good.normal.review.scheduled_days == 3


def test_rwkv_curve_states_only_send_supplied_ratings() -> None:
    requests: list[scheduler_pb2.SchedulingStatesWithIntervalsRequest] = []
    reviewer = _states_backend(requests, SchedulingStates())
    card = _rwkv_card(card_id=7, note_id=70, duration_millis=100)

    rwkv_curve_scheduling_states(
        reviewer,
        card,
        SchedulingStates(),
        RwkvIntervalOverride(good=10.5),
        RwkvIntervalOverride(hard=4, good=12.5),
    )

    assert not requests[0].HasField("hard")
    assert requests[0].good == pytest.approx(10.5)
    # an S90 goes only with its button's interval
    assert not requests[0].HasField("hard_s90")
    assert requests[0].good_s90 == pytest.approx(12.5)


def test_rwkv_curve_states_without_backend_use_whole_days() -> None:
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=None))
    card = _rwkv_card(card_id=7, note_id=70, duration_millis=100)
    states = SchedulingStates()
    states.hard.CopyFrom(_normal_review_state(interval=6, fuzz_delta=6))
    states.good.CopyFrom(_normal_review_state(interval=12, fuzz_delta=12))

    updated = rwkv_curve_scheduling_states(
        reviewer, card, states, RwkvIntervalOverride(hard=0.3, good=20.2)
    )

    assert updated.hard.normal.review.scheduled_days == 1
    assert updated.good.normal.review.scheduled_days == 21


def test_rwkv_curve_states_reject_invalid_intervals() -> None:
    requests: list[scheduler_pb2.SchedulingStatesWithIntervalsRequest] = []
    reviewer = _states_backend(requests, SchedulingStates())
    card = _rwkv_card(card_id=7, note_id=70, duration_millis=100)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            rwkv_curve_scheduling_states(
                reviewer, card, SchedulingStates(), RwkvIntervalOverride(good=bad)
            )
    assert requests == []


def test_unrounded_interval_from_recall_curve_keeps_sub_day_crossings() -> None:
    points = [
        RwkvRecallPoint(elapsed_days=1 / 24, retrievability=0.97),
        RwkvRecallPoint(elapsed_days=6 / 24, retrievability=0.85),
        RwkvRecallPoint(elapsed_days=1.0, retrievability=0.6),
    ]

    unrounded = unrounded_interval_from_recall_curve(
        points, 0.9, max_interval_days=36500
    )

    assert unrounded is not None and 1 / 24 < unrounded < 6 / 24
    # the whole-day variant rounds the same crossing up
    assert interval_from_recall_curve(points, 0.9, max_interval_days=36500) == 1


def _resident_interval_access(
    monkeypatch: pytest.MonkeyPatch,
    backend: object,
) -> None:
    """Route the fast reschedule path at `backend`, bypassing the global state."""

    from contextlib import contextmanager

    @contextmanager
    def access(*, expected_state_token: object = None) -> Iterator[object]:
        yield backend

    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler, "_try_reviewer_backend_prediction_access", access
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_state_generation", lambda b=None: 0
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_prediction_access_is_current",
        lambda backend, **kwargs: True,
    )


def test_reschedule_predictions_prefer_resident_current_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the fast reschedule path: query-only, resident-state predictions."""

    calls: list[list[RwkvReviewInput]] = []

    def predict_current_intervals_inputs_from_warm_up(
        review_inputs: list[RwkvReviewInput],
    ) -> list[RwkvReviewPrediction | None]:
        calls.append(list(review_inputs))
        return [
            RwkvReviewPrediction(
                retrievability=0.5, current_interval=7, current_s90=12
            ),
            None,
        ]

    backend = SimpleNamespace(
        supports_resident_current_intervals=True,
        predict_current_intervals_inputs_from_warm_up=(
            predict_current_intervals_inputs_from_warm_up
        ),
    )
    _resident_interval_access(monkeypatch, backend)
    first = _rwkv_review_input(card_id=1, note_id=10)
    second = _rwkv_review_input(card_id=2, note_id=20)

    predictions = rwkv_scheduler._rwkv_review_current_interval_predictions_for_inputs(
        [(1, first), (2, second)]
    )

    assert calls == [[first, second]]
    assert predictions == [
        RwkvReviewPrediction(retrievability=0.5, current_interval=7, current_s90=12),
        None,
    ]


def test_reschedule_predictions_report_unsupported_resident_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_input = _rwkv_review_input(card_id=1, note_id=10)

    # backend without the capability: never enters the prediction context
    _resident_interval_access(monkeypatch, SimpleNamespace())
    assert isinstance(
        rwkv_scheduler._rwkv_review_current_interval_predictions_for_inputs(
            [(1, review_input)]
        ),
        rwkv_scheduler._ResidentIntervalsUnavailable,
    )

    # backend that claims support but whose runtime declines at call time
    _resident_interval_access(
        monkeypatch,
        SimpleNamespace(
            supports_resident_current_intervals=True,
            predict_current_intervals_inputs_from_warm_up=lambda inputs: None,
        ),
    )
    assert isinstance(
        rwkv_scheduler._rwkv_review_current_interval_predictions_for_inputs(
            [(1, review_input)]
        ),
        rwkv_scheduler._ResidentIntervalsUnavailable,
    )


def test_backend_resident_current_intervals_require_runtime_support() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    review_input = _rwkv_review_input(card_id=1, note_id=10)

    # no runtime support -> None, so the caller falls back to the full path
    assert not backend.supports_resident_current_intervals
    assert backend.predict_current_intervals_inputs_from_warm_up([review_input]) is None

    def predict_current_intervals_many_from_warm_up(
        review_inputs: list[RwkvReviewInput],
    ) -> list[tuple[float, int, float, float]]:
        assert review_inputs == [review_input, review_input]
        return [(0.5, 7, 12.0, 6.2), (0.4, 0, 0.0, 0.0)]

    runtime.predict_current_intervals_many_from_warm_up = (  # type: ignore[attr-defined]
        predict_current_intervals_many_from_warm_up
    )
    assert backend.supports_resident_current_intervals
    assert backend.predict_current_intervals_inputs_from_warm_up(
        [review_input, review_input]
    ) == [
        RwkvReviewPrediction(
            retrievability=0.5,
            current_interval=7,
            current_interval_unrounded=6.2,
            current_s90=12,
        ),
        RwkvReviewPrediction(retrievability=0.4),
    ]
    assert backend.predict_current_intervals_inputs_from_warm_up([]) == []


def test_rust_runtime_current_intervals_map_zero_to_none() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    rows: list[tuple[object, ...]] = []

    def predict_current_intervals_many_from_warm_up(
        batch: list[tuple[object, ...]],
    ) -> list[tuple[float, int, float, float]]:
        rows.extend(batch)
        return [(0.5, 7, 12.0, 6.2), (0.4, 0, 0.0, 0.0)]

    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = SimpleNamespace(
        predict_current_intervals_many_from_warm_up=(
            predict_current_intervals_many_from_warm_up
        )
    )
    inputs = [
        _rwkv_review_input(card_id=1, note_id=10),
        _rwkv_review_input(card_id=2, note_id=20),
    ]

    outputs = runtime.predict_current_intervals_many_from_warm_up(inputs)

    assert len(rows) == 2 and rows[0][0] == 1 and rows[1][0] == 2
    assert outputs == [(0.5, 7, 12, 6.2), (0.4, None, None, None)]


def test_reviewer_rwkv_curve_intervals_go_through_review_fuzz() -> None:
    """Pins spec/scheduling.md#sched.rwkv-curve-fuzz end to end in the reviewer."""

    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=4,
                    good=9,
                    easy=18,
                ),
            )

    requests: list[scheduler_pb2.SchedulingStatesWithIntervalsRequest] = []

    def scheduling_states_with_intervals(
        request: scheduler_pb2.SchedulingStatesWithIntervalsRequest,
    ) -> SchedulingStates:
        requests.append(request)
        rebuilt = SchedulingStates()
        rebuilt.again.CopyFrom(_normal_review_state(interval=1, fuzz_delta=0))
        rebuilt.hard.CopyFrom(_normal_review_state(interval=5, fuzz_delta=1))
        rebuilt.good.CopyFrom(_normal_review_state(interval=8, fuzz_delta=-1))
        rebuilt.easy.CopyFrom(_normal_review_state(interval=20, fuzz_delta=2))
        return rebuilt

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer()
    reviewer.mw.col._backend = SimpleNamespace(
        scheduling_states_with_intervals=scheduling_states_with_intervals
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.again.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.hard.CopyFrom(_normal_review_state(interval=6, fuzz_delta=6))
    states.good.CopyFrom(_normal_review_state(interval=12, fuzz_delta=12))
    states.easy.CopyFrom(_normal_review_state(interval=24, fuzz_delta=24))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    # the RWKV-Curve targets were sent to the backend for this card...
    assert len(requests) == 1
    assert requests[0].card_id == 1
    assert (
        requests[0].again,
        requests[0].hard,
        requests[0].good,
        requests[0].easy,
    ) == (
        1,
        4,
        9,
        18,
    )
    # ...and the backend's states, fuzz deltas included, are what the reviewer uses
    assert updated.again.normal.review.scheduled_days == 1
    assert updated.again.normal.review.fuzz_delta_days == 0
    assert updated.hard.normal.review.scheduled_days == 5
    assert updated.hard.normal.review.fuzz_delta_days == 1
    assert updated.good.normal.review.scheduled_days == 8
    assert updated.good.normal.review.fuzz_delta_days == -1
    assert updated.easy.normal.review.scheduled_days == 20
    assert updated.easy.normal.review.fuzz_delta_days == 2


def test_reviewer_rwkv_prediction_uses_reviews_of_other_cards() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    before = update_reviewer_scheduling_states(states, reviewer, card_b)
    record_reviewer_answer(reviewer, card_a, ease=3)
    after = update_reviewer_scheduling_states(states, reviewer, card_b)

    assert before.good.normal.review.scheduled_days == 5
    assert after.good.normal.review.scheduled_days == 6
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.55)
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card_b,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.55)
    assert diagnostics.retrievability_source == "RWKV"
    assert (
        rwkv_card_info_rows(
            reviewer=reviewer,
            card=card_b,
            fallback_source="FSRS",
        )
        == []
    )
    assert runtime.reviewed == [(1, 3)]
    assert runtime.queries == [
        (2, None, None),
        (2, 1, ("deck", 100, 1)),
    ]
    assert runtime.query_inputs[0].is_query is True
    assert runtime.query_inputs[0].ease is None
    assert runtime.query_inputs[0].duration_millis is None
    assert runtime.query_inputs[0].identity.preset_id == 1000
    assert runtime.query_inputs[0].day_offset == 42
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days is None
    assert runtime.answered_inputs[0].is_query is False
    assert runtime.answered_inputs[0].ease == 3
    assert runtime.answered_inputs[0].duration_millis == 1234
    assert runtime.answered_inputs[0].reps == 5
    assert runtime.answered_inputs[0].lapses == 1
    assert states.good.normal.review.scheduled_days == 3


def test_reviewer_rwkv_prediction_overrides_all_grade_intervals_and_s90() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=4,
                    good=9,
                    easy=18,
                ),
                s90_overrides=RwkvIntervalOverride(
                    again=2,
                    hard=5,
                    good=10,
                    easy=20,
                ),
            )

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.again.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.hard.CopyFrom(_normal_review_state(interval=6, fuzz_delta=6))
    states.good.CopyFrom(_normal_review_state(interval=12, fuzz_delta=12))
    states.easy.CopyFrom(_normal_review_state(interval=24, fuzz_delta=24))
    states.again.normal.review.memory_state.stability = 30
    states.hard.normal.review.memory_state.stability = 60
    states.good.normal.review.memory_state.stability = 120
    states.easy.normal.review.memory_state.stability = 240

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated is not states
    assert updated.again.normal.review.scheduled_days == 1
    assert updated.hard.normal.review.scheduled_days == 4
    assert updated.good.normal.review.scheduled_days == 9
    assert updated.easy.normal.review.scheduled_days == 18
    assert updated.again.normal.review.memory_state.stability == pytest.approx(2)
    assert updated.hard.normal.review.memory_state.stability == pytest.approx(5)
    assert updated.good.normal.review.memory_state.stability == pytest.approx(10)
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(20)
    assert states.again.normal.review.scheduled_days == 3
    assert states.hard.normal.review.scheduled_days == 6
    assert states.good.normal.review.scheduled_days == 12
    assert states.easy.normal.review.scheduled_days == 24
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


def test_live_scheduling_skips_prediction_while_backend_execution_is_busy() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls = 0

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.calls += 1
            return RwkvReviewPrediction(
                interval_overrides=RwkvIntervalOverride(good=30),
            )

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_execution_lock() -> None:
        with rwkv_scheduler._reviewer_backend_execution_lock:
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    thread = threading.Thread(target=hold_execution_lock)
    thread.start()
    try:
        assert lock_acquired.wait(timeout=5)
        updated = update_reviewer_scheduling_states(states, reviewer, card)
    finally:
        release_lock.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert updated is states
    assert backend.calls == 0
    assert states.good.normal.review.scheduled_days == 3


def test_live_scheduling_discards_prediction_after_backend_replacement() -> None:
    class Replacement:
        pass

    replacement = Replacement()

    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            set_reviewer_backend(cast(Any, replacement))
            return RwkvReviewPrediction(
                interval_overrides=RwkvIntervalOverride(good=30),
            )

    set_reviewer_backend(cast(Any, Backend()))
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated is states
    assert states.good.normal.review.scheduled_days == 3
    assert rwkv_scheduler._reviewer_backend is replacement


@pytest.mark.parametrize(
    "enforce_grade_order",
    [True, False],
)
def test_reviewer_rwkv_grade_order_setting_is_passed_to_the_curve_computation(
    enforce_grade_order: bool,
) -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=17,
                    hard=245,
                    good=2,
                    easy=1_035,
                ),
                s90_overrides=RwkvIntervalOverride(
                    again=14,
                    hard=60,
                    good=3,
                    easy=90,
                ),
            )

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer(
        rwkv_review_enforce_grade_order=enforce_grade_order,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    identity = rwkv_review_identity(reviewer, card)
    assert identity is not None
    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=identity,
        ease=None,
    )
    states = SchedulingStates()
    for rating in ("again", "hard", "good", "easy"):
        getattr(states, rating).CopyFrom(_normal_review_state(interval=1, fuzz_delta=0))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert review_input.enforce_grade_order is enforce_grade_order
    assert tuple(
        getattr(updated, rating).normal.review.scheduled_days
        for rating in ("again", "hard", "good", "easy")
    ) == (17, 245, 2, 1_035)
    assert tuple(
        round(getattr(updated, rating).normal.review.memory_state.stability)
        for rating in ("again", "hard", "good", "easy")
    ) == (14, 60, 3, 90)


def test_rwkv_review_input_uses_preset_desired_retention_for_all_grade_targets() -> (
    None
):
    reviewer = _rwkv_reviewer(preset_desired_retention=0.86)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.target_retentions == pytest.approx((0.86, 0.86, 0.86, 0.86))


def test_rwkv_review_input_falls_back_to_deck_desired_retention() -> None:
    reviewer = _rwkv_reviewer(
        resolved_preset_id=None,
        deck_desired_retention=0.82,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.target_retentions == pytest.approx((0.82, 0.82, 0.82, 0.82))


def test_rwkv_review_input_encodes_filtered_state_like_training_data() -> None:
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.filtered.preview.scheduled_secs = 60
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=3,
    )

    assert review_input.current_state_kind == "filtered"
    assert review_input.card_type == int(rwkv_scheduler.RwkvReviewState.FILTERED)


@pytest.mark.parametrize(
    ("review_kind", "expected_state"),
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)],
)
def test_historical_review_kind_maps_to_training_dataset_state(
    review_kind: int,
    expected_state: int,
) -> None:
    assert rwkv_scheduler._historical_review_state(review_kind) == expected_state


RATED_LEARNING = (3, 0, 2500)
RATED_REVIEW = (3, 1, 2500)
RATED_RELEARNING = (1, 2, 2500)
FORGET = (0, 4, 0)


def _replay_db(revlog: Sequence[tuple[int, int, tuple[int, int, int]]]) -> object:
    """An in-memory collection holding `(review id, card id, kind)` rows.

    The tests below run the replay's real SQL against it, so they pin the one
    implementation of `sched.rwkv-replay-start-row` rather than a Python copy
    of the rule.
    """
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table cards (id integer primary key, nid integer, did integer, "
        "odid integer)"
    )
    connection.execute(
        "create table revlog (id integer primary key, cid integer, ease integer, "
        "ivl integer, factor integer, time integer, type integer)"
    )
    for card_id in sorted({card_id for _, card_id, _ in revlog}):
        connection.execute(
            "insert into cards (id, nid, did, odid) values (?, ?, 100, 0)",
            (card_id, card_id * 10),
        )
    for review_id, card_id, (ease, kind, factor) in revlog:
        connection.execute(
            "insert into revlog (id, cid, ease, ivl, factor, time, type) "
            "values (?, ?, ?, 5, ?, 100, ?)",
            (review_id, card_id, ease, factor, kind),
        )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            return cast(
                list[tuple[object, ...]], connection.execute(sql, args).fetchall()
            )

    return SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))


def _replay_start_rows(
    reviewer: object,
    *,
    card_ids: Sequence[int] | None = None,
) -> list[tuple[int, int]]:
    """`(review id, state code)` of every replayed row, in replay order."""
    rows = rwkv_scheduler._historical_rwkv_review_rows(reviewer, card_ids=card_ids)
    return [
        (cast(int, row[0]), state)
        for row, state in rwkv_scheduler._retained_historical_review_rows(rows)
    ]


def test_historical_learning_start_resets_and_review_only_history_is_retained() -> None:
    reviewer = _replay_db(
        [
            (1_000, 1, RATED_LEARNING),
            (2_000, 1, RATED_REVIEW),
            (3_000, 1, RATED_LEARNING),
            (4_000, 1, RATED_LEARNING),
            (5_000, 1, RATED_REVIEW),
            (6_000, 2, RATED_REVIEW),
        ]
    )

    assert _replay_start_rows(reviewer) == [
        (3_000, 0),
        (4_000, int(RwkvReviewState.LEARNING)),
        (5_000, int(RwkvReviewState.REVIEW)),
        # Card 2 has no Learning row, so its first rated row is the start row
        # and carries the learn-start state, not REVIEW.
        (6_000, 0),
    ]


def test_historical_fallback_start_row_gets_the_learn_start_state() -> None:
    """A card with no Learning row still gets the learn-start state.

    `sched.rwkv-replay-start-row`: the start row always carries the learn-start
    code, whatever its own kind, so the model sees the same first row it saw in
    training.
    """
    reviewer = _replay_db(
        [
            (1_000, 1, RATED_REVIEW),
            (2_000, 1, RATED_RELEARNING),
            (3_000, 1, RATED_REVIEW),
        ]
    )

    assert _replay_start_rows(reviewer) == [
        (1_000, 0),
        (2_000, int(RwkvReviewState.RELEARNING)),
        (3_000, int(RwkvReviewState.REVIEW)),
    ]


def test_historical_replay_drops_the_rows_before_a_fallback_card_forget() -> None:
    """The Forget cut reaches the Python replay, not only the backend query."""
    reviewer = _replay_db(
        [
            (1_000, 1, RATED_REVIEW),
            (2_000, 1, FORGET),
            (3_000, 1, RATED_REVIEW),
            (4_000, 1, RATED_RELEARNING),
        ]
    )

    assert _replay_start_rows(reviewer) == [
        (3_000, 0),
        (4_000, int(RwkvReviewState.RELEARNING)),
    ]


def test_historical_replay_keeps_a_learning_start_over_a_later_forget() -> None:
    """Rule 1 wins, here as in the backend query."""
    reviewer = _replay_db(
        [
            (1_000, 1, RATED_LEARNING),
            (2_000, 1, RATED_REVIEW),
            (3_000, 1, FORGET),
            (4_000, 1, RATED_REVIEW),
        ]
    )

    assert _replay_start_rows(reviewer) == [
        (1_000, 0),
        (2_000, int(RwkvReviewState.REVIEW)),
        (4_000, int(RwkvReviewState.REVIEW)),
    ]


def test_grade_now_and_the_replay_agree_on_a_forgotten_fallback_card() -> None:
    """The acceptance test for one start rule with one implementation.

    A card with no Learning row and a Forget in the middle must give Grade Now
    and the reviewer's replay the same start row and the same state. Both read
    their rows from `_historical_rwkv_review_rows`, which carries the rule in
    its SQL; Grade Now differs only in filtering to the graded card.
    """
    reviewer = _replay_db(
        [
            (1_000, 1, RATED_REVIEW),
            (2_000, 1, RATED_REVIEW),
            (3_000, 1, FORGET),
            (4_000, 1, RATED_REVIEW),
            (5_000, 1, RATED_RELEARNING),
            (6_000, 2, RATED_LEARNING),
        ]
    )

    replay = [entry for entry in _replay_start_rows(reviewer) if entry[0] < 6_000]
    grade_now = _replay_start_rows(reviewer, card_ids=[1])

    assert replay == grade_now
    assert grade_now == [
        (4_000, 0),
        (5_000, int(RwkvReviewState.RELEARNING)),
    ]

    # The Grade Now history helper reads the same rows and keeps the same start.
    histories = rwkv_scheduler._rwkv_grade_now_card_histories(
        rwkv_scheduler._historical_rwkv_review_rows(reviewer, card_ids=[1])
    )
    assert histories[1].review_count == 2


@pytest.mark.parametrize(
    ("previous_kind", "previous_ease", "expected_state"),
    [
        (1, 1, RwkvReviewState.RELEARNING),
        (1, 3, RwkvReviewState.FILTERED),
        (2, 1, RwkvReviewState.RELEARNING),
        (2, 2, RwkvReviewState.RELEARNING),
        (2, 3, RwkvReviewState.FILTERED),
        (2, 4, RwkvReviewState.FILTERED),
        (0, 3, RwkvReviewState.FILTERED),
        (3, 3, RwkvReviewState.REVIEW),
    ],
)
def test_live_same_day_review_uses_scheduler_valid_synthetic_state(
    previous_kind: int,
    previous_ease: int,
    expected_state: RwkvReviewState,
) -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(
        first=lambda sql, card_id: (previous_id, previous_ease, previous_kind)
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(expected_state)


def test_live_interday_rwkv_answer_remains_review() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (41 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 1, 1))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(RwkvReviewState.REVIEW)


def test_live_review_after_explicit_filtered_answer_respects_scheduler_state() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 3))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(RwkvReviewState.REVIEW)


def test_rwkv_review_input_uses_exact_elapsed_for_review_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 30,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.current_elapsed_days == 0
    assert review_input.current_elapsed_seconds == 30


def test_rwkv_review_input_uses_exact_elapsed_for_filtered_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.filtered.rescheduling.original_state.review.elapsed_days = 7
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 30,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.card_type == 4
    assert review_input.current_state_kind == "filtered"
    assert review_input.current_normal_state_kind is None
    assert review_input.current_elapsed_days == 0
    assert review_input.current_elapsed_seconds == 30


# Pins spec/scheduling.md#sched.rwkv-exact-elapsed: a learning card's elapsed
# time is the time since its last review, not since the step came due.
def test_rwkv_review_input_uses_exact_elapsed_for_learning_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    # the state counts from the card's due time: 30 seconds, not 90
    reviewer._v3.states.current.normal.learning.elapsed_secs = 30
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 90,
    )
    card.type = 1
    card.queue = 1

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.card_type == int(RwkvReviewState.LEARNING)
    assert review_input.current_elapsed_days == 0
    assert review_input.current_elapsed_seconds == 90


def test_rwkv_review_input_keeps_state_elapsed_for_learning_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.normal.learning.elapsed_secs = 300

    class DB:
        def first(self, sql: str, card_id: int) -> None:
            return None

    reviewer.mw.col.db = DB()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card.type = 1
    card.queue = 1

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.current_elapsed_days is None
    assert review_input.current_elapsed_seconds == 300


def test_rwkv_review_input_falls_back_to_latest_eligible_review_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    previous_review_time = 41 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()

    class DB:
        def first(self, sql: str, card_id: int) -> tuple[int, int, int]:
            assert "from revlog" in sql
            assert card_id == 1
            return previous_review_time * 1000, 3, 1

    reviewer.mw.col.db = DB()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.card_type == 2
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 86_400


def test_rwkv_review_input_uses_card_creation_elapsed_for_new_cards() -> None:
    reviewer = _rwkv_reviewer(rwkv_review_first_review_elapsed_from_card_creation=True)
    reviewer._v3.states.current.normal.new.SetInParent()
    card = _rwkv_card(
        card_id=(42 * 86_400 + 100 - 90_000) * 1000,
        note_id=10,
        duration_millis=1234,
    )
    card.type = 0
    card.queue = 0

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=card.id,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_rwkv_first_learning_answer_does_not_store_card_creation_elapsed() -> None:
    query = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=int(RwkvReviewState.LEARN_START),
        current_elapsed_days=3,
        current_elapsed_seconds=3 * 86_400,
    )
    answer = replace(query, is_query=False, ease=3, duration_millis=1234)

    state_update = rwkv_scheduler._rwkv_state_update_input(answer)

    assert query.current_elapsed_days == 3
    assert query.current_elapsed_seconds == 3 * 86_400
    assert state_update.current_elapsed_days == -1
    assert state_update.current_elapsed_seconds == -1


def test_rwkv_later_learning_answer_preserves_elapsed_time() -> None:
    answer = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        is_query=False,
        ease=3,
        duration_millis=1234,
        card_type=int(RwkvReviewState.LEARNING),
        current_elapsed_days=0,
        current_elapsed_seconds=600,
    )

    assert rwkv_scheduler._rwkv_state_update_input(answer) is answer


# Pins spec/deck-options.md#deck-options.rwkv-fixed-settings
def test_rwkv_review_input_uses_card_creation_even_if_stored_off() -> None:
    reviewer = _rwkv_reviewer(rwkv_review_first_review_elapsed_from_card_creation=False)
    reviewer._v3.states.current.normal.new.SetInParent()
    card = _rwkv_card(
        card_id=(42 * 86_400 + 100 - 90_000) * 1000,
        note_id=10,
        duration_millis=1234,
    )
    card.type = 0
    card.queue = 0

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=card.id,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.current_normal_state_kind == "new"
    # the time since the card was created, although the preset stores off
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_rwkv_stats_graph_review_input_uses_exact_elapsed_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: 10_000.0)
    card = rwkv_scheduler.RwkvStatsGraphCard(
        id=1,
        nid=10,
        did=100,
        odid=0,
        type=2,
        queue=2,
        due=50,
        odue=0,
        ivl=4,
        factor=2500,
        reps=5,
        lapses=1,
        last_review_time=9_970,
    )

    review_input = rwkv_scheduler._rwkv_review_input_for_stats_graph_card(
        card=card,
        deck_config={"id": 1000, "rwkvReviewEnabled": True},
        timing=SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400),
    )

    assert review_input is not None
    assert review_input.current_elapsed_seconds == 30


def test_rwkv_stats_graph_review_input_uses_card_creation_elapsed_by_default() -> None:
    now = 42 * 86_400 + 100
    card = rwkv_scheduler.RwkvStatsGraphCard(
        id=(now - 90_000) * 1000,
        nid=10,
        did=100,
        odid=0,
        type=0,
        queue=0,
        due=50,
        odue=0,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        last_review_time=None,
    )

    review_input = rwkv_scheduler._rwkv_review_input_for_stats_graph_card(
        card=card,
        deck_config={
            "id": 1000,
            "rwkvReviewEnabled": True,
        },
        timing=SimpleNamespace(
            now=now,
            days_elapsed=42,
            next_day_at=43 * 86_400,
        ),
    )

    assert review_input is not None
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_record_reviewer_answer_does_not_write_card_s90_separately() -> None:
    class Backend:
        def __init__(self) -> None:
            self.answers: list[tuple[int, int]] = []

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            self.answers.append((card.id, ease))

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    rwkv_scheduler._reviewer_backend_warmup_states[
        (id(backend), id(reviewer.mw.col))
    ] = None
    reviewer.mw.col.update_card = lambda card, skip_undo_entry=False: pytest.fail(
        "unexpected card update"
    )
    reviewer.mw.col.db = SimpleNamespace(
        execute=lambda *args, **kwargs: pytest.fail("unexpected DB execute"),
        executemany=lambda *args, **kwargs: pytest.fail("unexpected DB executemany"),
        scalar=lambda *args, **kwargs: pytest.fail("unexpected DB scalar"),
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card.load = lambda: pytest.fail("unexpected card reload")
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=RwkvIntervalOverride(
            again=2,
            hard=5,
            good=10,
            easy=20,
        ),
    )

    record_reviewer_answer(reviewer, card, ease=4)

    assert backend.answers == [(1, 4)]


def test_set_answer_rwkv_metadata_sets_retrievability_and_selected_s90() -> None:
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    answer = SimpleNamespace()
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=RwkvIntervalOverride(
            again=2,
            hard=5,
            good=10,
            easy=20,
        ),
    )

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=4)

    assert answer.rwkv_s90 == pytest.approx(20)
    assert answer.rwkv_retrievability == pytest.approx(0.62)
    assert answer.rwkv_review_kind == 1


def _curve_prediction(card_id: int = 1) -> RwkvReviewerPrediction:
    return RwkvReviewerPrediction(
        card_id=card_id,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=RwkvIntervalOverride(again=2, hard=5, good=10, easy=20),
    )


# Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait: a prediction serves
# one answer; a later showing of the card cannot reuse its S90.
def test_set_answer_rwkv_metadata_clears_the_prediction() -> None:
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    reviewer._rwkv_review_prediction = _curve_prediction()

    rwkv_scheduler.set_answer_rwkv_metadata(SimpleNamespace(), reviewer, card, ease=4)
    again = SimpleNamespace()
    rwkv_scheduler.set_answer_rwkv_metadata(again, reviewer, card, ease=4)

    assert reviewer._rwkv_review_prediction is None
    assert not hasattr(again, "rwkv_s90")
    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)


# Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait
def test_answer_intervals_pending_until_rwkv_curve_gives_the_intervals() -> None:
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)
    reviewer._rwkv_review_prediction = _curve_prediction(card_id=2)
    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)
    reviewer._rwkv_review_prediction = replace(
        _curve_prediction(), interval_override_used=False
    )
    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)
    reviewer._rwkv_review_prediction = _curve_prediction()
    assert not rwkv_scheduler.answer_intervals_pending(reviewer, card)

    # a preview in a filtered deck without rescheduling has no intervals
    reviewer._rwkv_review_prediction = None
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.filtered.preview.scheduled_secs = 60
    assert not rwkv_scheduler.answer_intervals_pending(reviewer, card)

    # FSRS-7 and RWKV-Instant presets never wait
    for fsrs_or_instant in (
        _rwkv_reviewer(rwkv_review_enabled=False),
        _rwkv_reviewer(
            rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
        ),
    ):
        assert not rwkv_scheduler.answer_intervals_pending(fsrs_or_instant, card)


def test_answer_intervals_unavailable_only_when_rwkv_curve_answered() -> None:
    """Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait: a prediction with
    no interval for a button is permanent, so the reviewer says so at once."""
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    # no prediction yet: the wait can still end
    assert not rwkv_scheduler.answer_intervals_unavailable(reviewer, card)
    # a prediction of another card says nothing about this one
    reviewer._rwkv_review_prediction = _curve_prediction(card_id=2)
    assert not rwkv_scheduler.answer_intervals_unavailable(reviewer, card)
    # RWKV-Curve answered and gave no interval for a button
    reviewer._rwkv_review_prediction = replace(
        _curve_prediction(), interval_override_used=False
    )
    assert rwkv_scheduler.answer_intervals_unavailable(reviewer, card)
    # the intervals arrived
    reviewer._rwkv_review_prediction = _curve_prediction()
    assert not rwkv_scheduler.answer_intervals_unavailable(reviewer, card)

    # FSRS-7 and RWKV-Instant presets never wait, so they never report this
    for fsrs_or_instant in (
        _rwkv_reviewer(rwkv_review_enabled=False),
        _rwkv_reviewer(
            rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
        ),
    ):
        fsrs_or_instant._rwkv_review_prediction = replace(
            _curve_prediction(), interval_override_used=False
        )
        assert not rwkv_scheduler.answer_intervals_unavailable(fsrs_or_instant, card)


def test_the_answer_button_wait_can_restore_the_resident_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait: the waiting
    buttons restore RWKV-Curve's state; only asking again never ends the
    wait, because the other review-time restore runs after an answer."""
    reviewer = _rwkv_reviewer()
    prepared: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_review",
        lambda target: prepared.append(target) or True,
    )

    assert rwkv_scheduler.prepare_reviewer_backend_for_answer_buttons(reviewer)

    assert prepared == [reviewer]


# Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait: a failed prediction
# leaves no stored prediction, so the buttons wait instead of showing FSRS's.
def test_failed_rwkv_prediction_leaves_the_buttons_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    reviewer._rwkv_review_prediction = _curve_prediction()
    states = SchedulingStates()
    states.CopyFrom(reviewer._v3.states)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)

    returned = rwkv_scheduler.update_reviewer_scheduling_states(states, reviewer, card)

    assert returned is states
    assert reviewer._rwkv_review_prediction is None
    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)


def test_error_building_rwkv_curve_states_leaves_the_buttons_waiting() -> None:
    """Pins spec/scheduling.md#sched.rwkv-curve-buttons-wait: when building the
    answer states from RWKV-Curve's intervals fails, no prediction is kept, so
    the buttons wait and no answer stores RWKV's S90 with FSRS-7's states."""

    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1, hard=4, good=9, easy=18
                ),
                s90_overrides=RwkvIntervalOverride(again=2, hard=5, good=10, easy=19),
            )

    def failing_build(
        request: scheduler_pb2.SchedulingStatesWithIntervalsRequest,
    ) -> SchedulingStates:
        raise RuntimeError("backend error while building the states")

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer()
    reviewer.mw.col._backend = SimpleNamespace(
        scheduling_states_with_intervals=failing_build
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.CopyFrom(reviewer._v3.states)

    returned = rwkv_scheduler.update_reviewer_scheduling_states(states, reviewer, card)

    assert returned is states
    assert rwkv_scheduler.answer_intervals_pending(reviewer, card)
    answer = SimpleNamespace(answered_at_millis=0)
    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=3)
    assert not hasattr(answer, "rwkv_s90")


def test_set_answer_rwkv_metadata_persists_same_day_relearning_kind() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 1, 1))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    answer = SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000)

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=3)

    assert answer.rwkv_review_kind == 2
    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=3,
    )
    assert review_input.card_type == int(RwkvReviewState.RELEARNING)
    assert review_input.card_queue == int(rwkv_scheduler.QUEUE_TYPE_DAY_LEARN_RELEARN)
    assert review_input.current_normal_state_kind == "relearning"


def test_failed_answer_retry_replaces_pending_rwkv_input() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (40 * 86_400 + 100) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 0))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1_234)

    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 50) * 1000),
        reviewer,
        card,
        ease=3,
    )

    # Simulate an answer operation failure followed by a retry after card changes.
    card.did = 200
    card.due = 75
    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000),
        reviewer,
        card,
        ease=3,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=200),
        ease=3,
    )
    assert review_input.identity.deck_id == 200
    assert review_input.card_due == 75


def test_ineligible_answer_retry_clears_pending_rwkv_input() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (40 * 86_400 + 100) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 0))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1_234)

    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 50) * 1000),
        reviewer,
        card,
        ease=3,
    )

    card.did = 200
    card.due = 75
    reviewer.mw.col.decks.config_dict_for_deck_id = lambda deck_id: {
        "id": deck_id * 10,
        "rwkvReviewEnabled": deck_id == 100,
    }
    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000),
        reviewer,
        card,
        ease=3,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=200),
        ease=3,
    )
    assert review_input.identity.deck_id == 200
    assert review_input.card_due == 75


def test_live_answer_after_card_reload_matches_historical_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_review_id = (40 * 86_400 + 100) * 1000 + 123
    answered_at_millis = (42 * 86_400 + 100) * 1000 + 987
    raw_duration_millis = 9_000
    persisted_duration_millis = 5_000
    monkeypatch.setattr(
        rwkv_scheduler.time,
        "time",
        lambda: answered_at_millis / 1000,
    )
    rows: list[tuple[object, ...]] = [
        (
            previous_review_id,
            1,
            10,
            100,
            3,
            1_234,
            0,
            4,
            2_500,
            # The query's `is_learning_start`.
            1,
        ),
    ]

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert "from revlog r" in sql
            assert args == ()
            return list(rows)

        def first(self, sql: str, card_id: int) -> tuple[int, int, int]:
            assert "from revlog" in sql
            assert card_id == 1
            row = rows[-1]
            return int(row[0]), int(row[4]), int(row[6])

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = DB()
    rwkv_scheduler._reviewer_backend_warmup_states[
        (id(backend), id(reviewer.mw.col))
    ] = None
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=raw_duration_millis,
        last_review_time=previous_review_id // 1000,
    )
    card.time_limit = lambda: persisted_duration_millis
    answer = SimpleNamespace(
        answered_at_millis=answered_at_millis,
        milliseconds_taken=raw_duration_millis,
    )

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=3)

    assert runtime.answered_inputs == []
    rows.append(
        (
            answered_at_millis,
            1,
            10,
            100,
            3,
            persisted_duration_millis,
            1,
            5,
            2_400,
            # Not the start row: the card already has one.
            0,
        )
    )
    # Simulate Card.load() after the answer operation has persisted the new row.
    card.last_review_time = answered_at_millis // 1000
    card.time_taken = lambda capped=True: raw_duration_millis + 2_000
    card.ivl = 5
    card.factor = 2_400
    card.reps = 6

    record_reviewer_answer(reviewer, card, ease=3)

    live = runtime.answered_inputs[-1]
    rebuilt = rwkv_scheduler._historical_rwkv_review_inputs(reviewer).reviews[-1]
    assert (
        live.identity,
        live.ease,
        live.duration_millis,
        live.card_type,
        live.day_offset,
        live.current_elapsed_days,
        live.current_elapsed_seconds,
    ) == (
        rebuilt.identity,
        rebuilt.ease,
        rebuilt.duration_millis,
        rebuilt.card_type,
        rebuilt.day_offset,
        rebuilt.current_elapsed_days,
        rebuilt.current_elapsed_seconds,
    )
    assert live.duration_millis == persisted_duration_millis
    assert (live.current_elapsed_days, live.current_elapsed_seconds) == (2, 172_800)


def test_live_synthetic_filtered_state_chains_within_reviewer_session() -> None:
    reviewer = _rwkv_reviewer()
    first_id = (42 * 86_400 + 50) * 1000
    second_id = (42 * 86_400 + 100) * 1000
    latest = [(first_id, 3, 1)]
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: latest[0])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    first_answer = SimpleNamespace(answered_at_millis=second_id)

    rwkv_scheduler.set_answer_rwkv_metadata(first_answer, reviewer, card, ease=3)
    record_reviewer_answer(reviewer, card, ease=3)

    assert first_answer.rwkv_review_kind == 3
    latest[0] = (second_id, 3, 3)
    next_answer = SimpleNamespace(answered_at_millis=second_id + 1_000)
    rwkv_scheduler.set_answer_rwkv_metadata(next_answer, reviewer, card, ease=3)
    assert next_answer.rwkv_review_kind == 3


def test_stateful_reviewer_backend_batches_runtime_predictions() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_requests: list[RwkvReviewPredictionRequest] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_requests.extend(requests)
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                    interval_overrides=RwkvIntervalOverride(
                        again=7 + request.review_input.identity.card_id,
                        hard=8 + request.review_input.identity.card_id,
                        good=10 + request.review_input.identity.card_id,
                        easy=14 + request.review_input.identity.card_id,
                    ),
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    card_c = _rwkv_card(card_id=3, note_id=30, duration_millis=6789)
    backend.review_answered(reviewer=reviewer, card=card_a, ease=3)

    predictions = backend.predict_reviews(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_b),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ]
    )

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    first, second = [prediction for prediction in predictions if prediction]
    assert first.interval_overrides == RwkvIntervalOverride(
        again=9,
        hard=10,
        good=12,
        easy=16,
    )
    assert second.interval_overrides == RwkvIntervalOverride(
        again=10,
        hard=11,
        good=13,
        easy=17,
    )
    assert runtime.queries == []
    assert [
        (
            request.review_input.identity.card_id,
            request.review_input.is_query,
            request.global_state,
            request.deck_state,
        )
        for request in runtime.batch_requests
    ] == [
        (2, True, 1, ("deck", 100, 1)),
        (3, True, 1, ("deck", 100, 1)),
    ]


def test_stateful_reviewer_backend_caches_batch_query_predictions() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                    interval_overrides=RwkvIntervalOverride(
                        good=10 + request.review_input.identity.card_id
                    ),
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    candidates = [
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=2, note_id=20, duration_millis=5678),
        ),
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=3, note_id=30, duration_millis=6789),
        ),
    ]

    first = backend.predict_reviews(candidates)
    second = backend.predict_reviews(candidates)

    assert [prediction.retrievability for prediction in first if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert [prediction.retrievability for prediction in second if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert runtime.batch_card_ids == [[2, 3]]


def test_rwkv_review_scores_batches_only_prediction_cache_misses() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=2, note_id=20, duration_millis=2345)
    card_b = _rwkv_card(card_id=3, note_id=30, duration_millis=3456)
    card_c = _rwkv_card(card_id=4, note_id=40, duration_millis=4567)

    backend.predict_reviews(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_a),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ]
    )
    runtime.batch_card_ids.clear()

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_a),
            RwkvReviewCandidate(reviewer=reviewer, card=card_b),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ],
        batch_size=1,
    )

    assert scores == [
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
        (4, pytest.approx(0.40)),
    ]
    assert runtime.batch_card_ids == [[3]]


def test_rwkv_review_scores_use_retrievability_only_batch_path() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.retrievability_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                0.10 * request.review_input.identity.card_id for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
            ),
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(card_id=3, note_id=30, duration_millis=3456),
            ),
        ],
        batch_size=512,
    )

    assert scores == [
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
    ]
    assert runtime.retrievability_card_ids == [[2, 3]]


def test_rwkv_review_input_scores_use_resident_state_and_cache() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    inputs = [
        (2, _rwkv_review_input(card_id=2, note_id=20)),
        (3, _rwkv_review_input(card_id=3, note_id=30)),
    ]

    first = rwkv_scheduler._rwkv_review_scores_for_inputs(inputs, batch_size=1)
    second = rwkv_scheduler._rwkv_review_scores_for_inputs(inputs, batch_size=1)

    assert first == [(2, pytest.approx(0.20)), (3, pytest.approx(0.30))]
    assert second == first
    assert runtime.resident_card_ids == [[2, 3]]


def test_rwkv_current_retrievability_requests_use_resident_state() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []
            self.serialized_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.serialized_card_ids.append(card_ids)
            return [0.20 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    review_input = _rwkv_review_input(card_id=2, note_id=20)
    request = backend._prediction_request(review_input.identity, review_input)

    current = backend.predict_retrievability_requests([request])
    snapshot = backend.predict_retrievability_requests(
        [replace(request, card_state=b"older-state")]
    )

    assert [prediction.retrievability for prediction in current] == [
        pytest.approx(0.20)
    ]
    assert [prediction.retrievability for prediction in snapshot] == [
        pytest.approx(0.40)
    ]
    assert runtime.resident_card_ids == [[2]]
    assert runtime.serialized_card_ids == [[2]]


def test_rwkv_review_scores_upscale_default_retrievability_batch_size() -> None:
    assert rwkv_scheduler._rwkv_retrievability_batch_size(512) == 2048
    assert rwkv_scheduler._rwkv_retrievability_batch_size(8192) == 8192

    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.retrievability_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [0.50 for _ in requests]

    def candidates(count: int) -> list[RwkvReviewCandidate]:
        reviewer = _rwkv_reviewer()
        return [
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(
                    card_id=card_id,
                    note_id=card_id * 10,
                    duration_millis=1000 + card_id,
                ),
            )
            for card_id in range(1, count + 1)
        ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates(600),
        batch_size=512,
    )

    assert len(scores) == 600
    assert runtime.retrievability_card_ids == [list(range(1, 601))]

    runtime.retrievability_card_ids.clear()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates(130),
        batch_size=64,
    )

    assert len(scores) == 130
    assert runtime.retrievability_card_ids == [
        list(range(1, 65)),
        list(range(65, 129)),
        [129, 130],
    ]


def test_rwkv_retrievability_batches_do_not_populate_prediction_cache() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    reviewer = _rwkv_reviewer()
    candidates = [
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
        ),
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=3, note_id=30, duration_millis=3456),
        ),
    ]
    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)

    first = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates,
        batch_size=512,
    )
    second = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates,
        batch_size=512,
    )

    assert first == [(2, pytest.approx(0.20)), (3, pytest.approx(0.30))]
    assert second == first
    assert runtime.retrievability_card_ids == [[2, 3], [2, 3]]


def test_rwkv_retrievability_batch_cache_does_not_hide_full_intervals() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.full_card_ids: list[list[int]] = []
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.full_card_ids.append(card_ids)
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * card_id,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=10 + card_id,
                        easy=20 + card_id,
                    ),
                )
                for card_id in card_ids
            ]

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    candidate = RwkvReviewCandidate(
        reviewer=reviewer,
        card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
    )

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [candidate],
        batch_size=512,
    )
    prediction = backend.predict_reviews([candidate])[0]

    assert scores == [(2, pytest.approx(0.20))]
    assert prediction is not None
    assert prediction.interval_overrides.good == 12
    assert runtime.retrievability_card_ids == [[2]]
    assert runtime.full_card_ids == [[2]]


def test_stateful_reviewer_backend_clears_prediction_cache_after_answer() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    reviewed_card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    queried_card = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    before = backend.predict_review(reviewer=reviewer, card=queried_card)
    cached_before = backend.predict_review(reviewer=reviewer, card=queried_card)
    backend.review_answered(reviewer=reviewer, card=reviewed_card, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=queried_card)

    assert before is not None
    assert cached_before is not None
    assert after is not None
    assert before.retrievability == pytest.approx(0.45)
    assert cached_before.retrievability == pytest.approx(0.45)
    assert after.retrievability == pytest.approx(0.55)
    assert runtime.queries == [
        (2, None, None),
        (2, 1, ("deck", 100, 1)),
    ]


def test_stateful_reviewer_backend_reuses_curve_prediction_after_undo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.full_prediction_batches: list[list[int]] = []

        def predict_many(
            self,
            requests: Sequence[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction | None]:
            self.full_prediction_batches.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                self.review(
                    review_input=request.review_input,
                    card_state=request.card_state,
                    note_state=request.note_state,
                    deck_state=request.deck_state,
                    preset_state=request.preset_state,
                    global_state=request.global_state,
                ).prediction
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=900,
    )
    now = 1_000
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: now)

    first_states = SchedulingStates()
    first_states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    first = update_reviewer_scheduling_states(first_states, reviewer, card)

    counter.set(1)
    record_reviewer_answer(reviewer, card, ease=3)
    assert record_collection_undo(_undo_result(counter=1, next_counter=2)) == [1]
    now = 1_010

    restored_states = SchedulingStates()
    restored_states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    restored = update_reviewer_scheduling_states(restored_states, reviewer, card)

    assert first.good.normal.review.scheduled_days == 5
    assert restored.good.normal.review.scheduled_days == 5
    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.45)
    assert runtime.full_prediction_batches == [[1]]


def test_reviewer_rwkv_warmup_replays_historical_reviews_once_before_prediction() -> (
    None
):
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
            (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
        ],
    )
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert runtime.queries == [(2, 2, ("deck", 100, 2))]
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.65)
    assert runtime.answered_inputs[0].day_offset == 40
    assert runtime.answered_inputs[0].current_elapsed_seconds == -1
    assert runtime.answered_inputs[1].current_elapsed_seconds == 90_000
    assert runtime.answered_inputs[1].current_elapsed_days == 1
    assert runtime.answered_inputs[1].card_type == 3
    assert runtime.answered_inputs[1].duration_millis == 2345


def test_historical_rwkv_inputs_can_use_card_creation_for_first_review_elapsed() -> (
    None
):
    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, card_id, 10, 100, 2, 1234, 0, 3, 2500),
            (second_review, card_id, 10, 100, 3, 2345, 2, 5, 2400),
        ],
    )

    missing = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    card_creation = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        first_review_elapsed_source=rwkv_scheduler.RwkvFirstReviewElapsedSource.CARD_CREATION,
    )
    deck_config = rwkv_scheduler._historical_rwkv_review_inputs(
        _rwkv_reviewer(
            rwkv_review_first_review_elapsed_from_card_creation=True,
            historical_review_rows=[
                (first_review, card_id, 10, 100, 2, 1234, 0, 3, 2500),
                (second_review, card_id, 10, 100, 3, 2345, 2, 5, 2400),
            ],
        )
    )

    # the fixture stores the setting off; it is ignored (spec
    # deck-options.rwkv-fixed-settings)
    assert missing.reviews[0].current_elapsed_seconds == 3 * 86_400
    assert missing.reviews[0].current_elapsed_days == 3
    assert card_creation.reviews[0].current_elapsed_seconds == 3 * 86_400
    assert card_creation.reviews[0].current_elapsed_days == 3
    assert card_creation.reviews[1].current_elapsed_seconds == 90_000
    assert card_creation.reviews[1].current_elapsed_days == 1
    assert deck_config.reviews[0].current_elapsed_seconds == 3 * 86_400
    assert deck_config.reviews[0].current_elapsed_days == 3


def test_historical_rwkv_inputs_do_not_use_creation_for_a_fallback_start() -> None:
    """A fallback start row gets the learn-start state but not the creation age.

    `sched.rwkv-replay-start-row`: a card with no Learning row starts at its
    first rated row, and that row carries the learn-start state so the model
    sees the first row it saw in training. The row is only the first row we
    hold, though, not the card's known first review, so the creation-age
    elapsed option must not reach it: the card's creation age would invent an
    interval that never happened.
    """
    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    reviewer = _rwkv_reviewer(
        rwkv_review_first_review_elapsed_from_card_creation=True,
    )
    # The last column is the query's own `is_learning_start`: this single
    # Review row is the card's fallback start row.
    rows = [(first_review, card_id, 10, 100, 3, 1234, 1, 3, 2500, 1)]
    reviewer.mw.col.db = SimpleNamespace(all=lambda _sql, *_args: rows)

    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)

    assert history.reviews[0].card_type == int(RwkvReviewState.LEARN_START)
    assert history.reviews[0].current_elapsed_days == -1
    assert history.reviews[0].current_elapsed_seconds == -1


def test_historical_rwkv_inputs_use_scheduler_days_for_elapsed_days() -> None:
    first_review = (40 * 86_400 + 86_300) * 1000
    second_review = (41 * 86_400 + 100) * 1000
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, 1, 10, 100, 3, 1234, 1, 3, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 5, 2500),
        ],
    )

    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)

    assert history.reviews[1].current_elapsed_seconds == 200
    assert history.reviews[1].current_elapsed_days == 1


def test_compare_rwkv_first_review_elapsed_metrics_reports_logloss_change() -> None:
    class ElapsedRuntime:
        def review(
            self,
            *,
            review_input: RwkvReviewInput,
            card_state: object | None,
            note_state: object | None,
            deck_state: object | None,
            preset_state: object | None,
            global_state: object | None,
        ) -> RwkvReviewTransition:
            del card_state, note_state, deck_state, preset_state, global_state
            if review_input.ease is None:
                return RwkvReviewTransition(
                    prediction=RwkvReviewPrediction(
                        retrievability=(
                            0.8 if review_input.current_elapsed_seconds == -1 else 0.2
                        ),
                    ),
                )

            return RwkvReviewTransition()

    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    set_reviewer_backend(RwkvStatefulReviewerBackend(ElapsedRuntime()))
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, card_id, 10, 100, 1, 1234, 0, 3, 2500),
        ],
    )

    comparison = rwkv_scheduler.compare_rwkv_first_review_elapsed_metrics(
        reviewer.mw,
    )

    assert comparison["available"] is True
    missing = comparison["missing"]
    card_creation = comparison["cardCreation"]
    assert isinstance(missing, dict)
    assert isinstance(card_creation, dict)
    assert missing["count"] == 1
    assert card_creation["count"] == 1
    assert missing["logLoss"] > card_creation["logLoss"]


def test_stateful_warmup_uses_creation_elapsed_only_for_initial_query() -> None:
    calls: list[RwkvReviewInput] = []

    class Runtime:
        def review(
            self,
            *,
            review_input: RwkvReviewInput,
            card_state: object | None,
            note_state: object | None,
            deck_state: object | None,
            preset_state: object | None,
            global_state: object | None,
        ) -> RwkvReviewTransition:
            del card_state, note_state, deck_state, preset_state, global_state
            calls.append(review_input)
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(retrievability=0.5)
            )

    first_learning = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        is_query=False,
        ease=3,
        duration_millis=1234,
        card_type=int(RwkvReviewState.LEARN_START),
        current_elapsed_days=3,
        current_elapsed_seconds=3 * 86_400,
    )
    backend = RwkvStatefulReviewerBackend(Runtime())

    backend.warm_up(
        [first_learning],
        review_ids=[123],
        prediction_recorder=lambda _review_id, _retrievability: None,
    )

    assert len(calls) == 2
    query, answer = calls
    assert query.is_query is True
    assert query.current_elapsed_days == 3
    assert query.current_elapsed_seconds == 3 * 86_400
    assert answer.is_query is False
    assert answer.current_elapsed_days == -1
    assert answer.current_elapsed_seconds == -1


# Pins spec/deck-options.md#deck-options.rwkv-fixed-settings
def test_reviewer_rwkv_warmup_ignores_stored_dynamic_preset_replay() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    third_review = (41 * 86_400 + 100) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        rwkv_review_dynamic_preset_replay=True,
        resolved_preset_id="addon:test:current",
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 20, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 30, 2400),
            (third_review, 1, 10, 100, 4, 3456, 1, 40, 2300),
        ],
    )
    reviewer.mw.col.get_config = lambda key: {
        "simulator_rules": [
            {
                "preset_id": "addon:test:young",
                "max_interval_days": 20.0,
            },
            {
                "preset_id": "addon:test:mature",
                "min_interval_days": 21.0,
            },
        ],
    }

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    # the add-on's rules are not replayed per review: every review keeps the
    # card's current preset
    assert [item.identity.preset_id for item in runtime.answered_inputs] == [
        _expected_preset_hash("addon:test:current"),
    ] * 3


def test_reviewer_rwkv_warmup_pins_resolved_preset_without_dynamic_replay() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        resolved_preset_id=None,
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 20, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 30, 2400),
        ],
    )
    resolved_card_ids: list[int] = []

    def resolve_preset(card_id: int) -> SimpleNamespace:
        resolved_card_ids.append(card_id)
        return SimpleNamespace(id="addon:test:current")

    reviewer.mw.col.fsrs_preset_for_card = resolve_preset
    reviewer.mw.col.get_config = lambda key: {
        "simulator_rules": [
            {
                "preset_id": "addon:test:young",
                "max_interval_days": 20.0,
            },
            {
                "preset_id": "addon:test:mature",
                "min_interval_days": 21.0,
            },
        ],
    }

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert [item.identity.preset_id for item in runtime.answered_inputs] == [
        _expected_preset_hash("addon:test:current"),
        _expected_preset_hash("addon:test:current"),
    ]
    assert resolved_card_ids == [1]


def test_reviewer_rwkv_prediction_skips_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated.good.normal.review.scheduled_days == 3
    assert runtime.reviewed == []
    assert runtime.queries == []
    assert current_reviewer_retrievability(reviewer, card) is None


def test_reviewer_prediction_does_not_restore_cache_during_card_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    restore_calls = 0

    def restore(_reviewer: object, **_kwargs: object) -> None:
        nonlocal restore_calls
        restore_calls += 1

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated.good.normal.review.scheduled_days == 3
    assert restore_calls == 0
    assert not rwkv_scheduler._reviewer_backend_warmup_pending(reviewer)


def test_reviewer_rwkv_answer_update_skips_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(historical_review_rows=[], rpc=rpc)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == []
    assert rpc.card_info_calls == [{"card_id": 1, "retrievability": None}]

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == [(1, 3)]
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": None},
        {"card_id": 1, "retrievability": None},
    ]


def test_waiting_reviewer_answer_preserves_state_until_backend_is_available() -> None:
    class Backend:
        def __init__(self) -> None:
            self.answers: list[tuple[int, int]] = []

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            self.answers.append((getattr(card, "id"), ease))

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.col.db = object()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    set_reviewer_backend(cast(Any, backend))
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[key] = identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[key] = (0, identity)
    rwkv_scheduler._set_rwkv_card_info_score(reviewer, 1, 0.45)

    release = threading.Event()
    holder = _start_multi_batch_execution_lock_holder(release)
    completed = threading.Event()

    def record_answer() -> None:
        record_reviewer_answer(
            reviewer,
            card,
            ease=3,
        )
        completed.set()

    worker = threading.Thread(target=record_answer)
    worker.start()
    try:
        assert not completed.wait(timeout=0.1)
        assert rwkv_scheduler._reviewer_backend_warmup_states[key] == identity
        assert rwkv_scheduler._rwkv_memorised_history_identity_cache[key] == (
            0,
            identity,
        )
    finally:
        release.set()
        holder.join(timeout=5)
        worker.join(timeout=5)

    assert completed.is_set()
    assert backend.answers == [(1, 3)]
    assert rwkv_scheduler._reviewer_backend_warmup_states[key] is None
    assert key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[key] == 1
    assert rpc.card_info_calls[-1] == {"card_id": 1, "retrievability": None}
    assert 1 not in rpc.active_scores


def test_waiting_reviewer_answer_preserves_replacement_backend_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.answers: list[tuple[int, int]] = []

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            self.answers.append((getattr(card, "id"), ease))

    original_backend = Backend()
    replacement_backend = Backend()
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = object()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    set_reviewer_backend(cast(Any, original_backend))
    pending_check_started = threading.Event()
    release_pending_check = threading.Event()
    original_pending = rwkv_scheduler._reviewer_backend_warmup_pending
    first_check = True

    def pending(current_reviewer: object) -> bool:
        nonlocal first_check
        if first_check:
            first_check = False
            pending_check_started.set()
            assert release_pending_check.wait(timeout=5)
        return original_pending(current_reviewer)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_pending",
        pending,
    )
    completed = threading.Event()

    def record_answer() -> None:
        record_reviewer_answer(reviewer, card, ease=3)
        completed.set()

    worker = threading.Thread(target=record_answer)
    worker.start()
    try:
        assert pending_check_started.wait(timeout=5)
        set_reviewer_backend(cast(Any, replacement_backend))
        replacement_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
        assert replacement_key is not None
        identity = _rwkv_resident_identity()
        with rwkv_scheduler._reviewer_backend_state_lock:
            rwkv_scheduler._reviewer_backend_warmup_states[replacement_key] = identity
            rwkv_scheduler._rwkv_memorised_history_identity_cache[replacement_key] = (
                0,
                identity,
            )
    finally:
        release_pending_check.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert completed.is_set()
    assert original_backend.answers == []
    assert replacement_backend.answers == []
    assert rwkv_scheduler._reviewer_backend_warmup_states[replacement_key] == identity
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[replacement_key] == (
        0,
        identity,
    )


@pytest.mark.parametrize("fail_after_replacement", [False, True])
def test_reviewer_answer_callback_preserves_replacement_backend_state(
    fail_after_replacement: bool,
) -> None:
    class ReplacementBackend:
        pass

    replacement_backend = ReplacementBackend()
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = object()
    identity = _rwkv_resident_identity()

    class ReplacingBackend:
        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            set_reviewer_backend(cast(Any, replacement_backend))
            replacement_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
            assert replacement_key is not None
            with rwkv_scheduler._reviewer_backend_state_lock:
                rwkv_scheduler._reviewer_backend_warmup_states[replacement_key] = (
                    identity
                )
                rwkv_scheduler._rwkv_memorised_history_identity_cache[
                    replacement_key
                ] = (0, identity)
            if fail_after_replacement:
                raise RuntimeError("simulated stale backend failure")

    set_reviewer_backend(cast(Any, ReplacingBackend()))

    record_reviewer_answer(
        reviewer,
        _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        ease=3,
    )

    replacement_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert replacement_key is not None
    assert rwkv_scheduler._reviewer_backend is replacement_backend
    assert rwkv_scheduler._reviewer_backend_warmup_states[replacement_key] == identity
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[replacement_key] == (
        0,
        identity,
    )
    assert (
        rwkv_scheduler._reviewer_backend_warmup_generations.get(
            replacement_key,
            0,
        )
        == 0
    )


def test_reviewer_answer_invalidates_in_flight_warmup_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None

    def restore(
        _reviewer: object,
        **_kwargs: object,
    ) -> rwkv_scheduler.RwkvResidentStateIdentity:
        record_reviewer_answer(
            reviewer,
            _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
            ease=3,
        )
        return _rwkv_resident_identity()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_restore_reviewer_backend_cache",
        restore,
    )

    assert rwkv_scheduler._prepare_reviewer_backend_from_cache(reviewer) is False
    assert runtime.reviewed == []
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1
    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_pending_generations


def test_reviewer_answer_failure_invalidates_resident_state() -> None:
    class Backend(RwkvStatefulReviewerBackend):
        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            raise RuntimeError("simulated review update failure")

    backend = Backend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    record_reviewer_answer(
        reviewer,
        _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        ease=3,
    )

    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1


def test_reviewer_answer_keeps_runtime_warm_but_clears_its_identity() -> None:
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )

    record_reviewer_answer(
        reviewer,
        _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        ease=3,
    )

    assert runtime.reviewed == [(1, 3)]
    assert rwkv_scheduler._reviewer_backend_warmed_up(reviewer)
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is None
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1


def test_reviewer_rwkv_answer_does_not_store_cache_after_answer(
    tmp_path,
) -> None:
    review_id = (42 * 86_400 + 1000) * 1000
    rows = [(review_id, 1, 10, 100, 3, 1234, 1, 5, 2500)]
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[key] = None
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    update_reviewer_scheduling_states(states, reviewer, card)
    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == [(1, 3)]
    assert reviewer.mw.col.rwkv_retrievability_rows == []


def test_reviewer_rwkv_warmup_reports_review_progress() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    progress: list[RwkvWarmUpProgress] = []

    backend.warm_up(
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        progress=progress.append,
    )

    assert progress == [
        RwkvWarmUpProgress(processed_reviews=0, total_reviews=2),
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=2),
        RwkvWarmUpProgress(processed_reviews=2, total_reviews=2),
    ]


def test_rust_rwkv_warmup_chunk_size_fills_state_only_wavefront() -> None:
    assert _rust_warmup_chunk_size(0) == 1
    assert _rust_warmup_chunk_size(2) == 2
    assert _rust_warmup_chunk_size(4000) == 4000
    assert _rust_warmup_chunk_size(50000) == 16_384
    assert _rust_warmup_chunk_size(200000) == 16_384


def test_rust_rwkv_calibration_chunk_size_preserves_progress_chunks() -> None:
    assert _rust_warmup_chunk_size(0, record_predictions=True) == 1
    assert _rust_warmup_chunk_size(2, record_predictions=True) == 1
    assert _rust_warmup_chunk_size(4000, record_predictions=True) == 40
    assert _rust_warmup_chunk_size(50000, record_predictions=True) == 16_384


# Pins spec/ui.md#ui.plain-progress-text
def test_reviewer_rwkv_warmup_progress_label_says_how_far_and_time_left() -> None:
    from aqt.utils import tr

    step = tr.qt_misc_review_history_reading()
    label = rwkv_scheduler._rwkv_replay_progress_label(
        step,
        RwkvWarmUpProgress(processed_reviews=2, total_reviews=4),
        elapsed_seconds=6,
    )

    assert label == tr.qt_misc_review_history_progress(
        step=step, done="2", total="4", remaining="6s"
    )
    assert "elapsed" not in label and "cache" not in label.lower()
    # before the first review is done no time is known
    assert rwkv_scheduler._rwkv_replay_progress_label(
        step,
        RwkvWarmUpProgress(processed_reviews=0, total_reviews=4),
        elapsed_seconds=1,
    ) == tr.qt_misc_review_history_progress_start(step=step, done="0", total="4")


def test_reviewer_rwkv_warmup_progress_label_formats_long_times() -> None:
    from aqt.utils import tr

    step = tr.qt_misc_review_history_reading()
    label = rwkv_scheduler._rwkv_replay_progress_label(
        step,
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=2),
        elapsed_seconds=3661,
    )

    assert label == tr.qt_misc_review_history_progress(
        step=step, done="1", total="2", remaining="1h 01m 01s"
    )


def test_rwkv_state_cache_keeps_only_eight_day_recovery_checkpoint() -> None:
    review_ids = [(day * 86_400 + 100) * 1000 for day in (0, 8, 12, 14, 15, 16)]

    assert rwkv_scheduler._rwkv_recovery_checkpoint_review_counts(review_ids) == [2]


def test_historical_rwkv_inputs_prepare_checkpoint_without_rehashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_ids = [(day * 86_400 + 100) * 1000 for day in (0, 8, 16)]
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (review_id, 1, 10, 100, 3, 1234, 1, 3, 2500) for review_id in review_ids
        ],
    )
    hash_review = rwkv_scheduler._rwkv_history_hash_after_review
    hashed_review_ids: list[int] = []

    def count_hashes(
        previous_hash: str,
        review_id: int,
        review: RwkvReviewInput,
    ) -> str:
        hashed_review_ids.append(review_id)
        return hash_review(previous_hash, review_id, review)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_history_hash_after_review",
        count_hashes,
    )
    # the builder hashes through `_RwkvHistoryHasher`, which keeps the digest
    # as bytes between reviews; it is watched too, so "each review is hashed
    # once" holds whichever path does the hashing
    update_hasher = rwkv_scheduler._RwkvHistoryHasher.update

    def count_hasher_updates(
        hasher: object,
        review_id: int,
        review: RwkvReviewInput,
    ) -> None:
        hashed_review_ids.append(review_id)
        update_hasher(hasher, review_id, review)  # type: ignore[arg-type]

    monkeypatch.setattr(
        rwkv_scheduler._RwkvHistoryHasher,
        "update",
        count_hasher_updates,
    )

    history = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        prepare_recovery_checkpoint=True,
    )
    checkpoint = history.prepared_checkpoint_histories[2]
    cursor = rwkv_scheduler._RwkvCheckpointHistoryCursor(history)

    assert cursor.advance(2) == checkpoint
    final = cursor.advance(3)
    assert hashed_review_ids == review_ids
    assert checkpoint.last_review_id == review_ids[1]
    assert checkpoint.review_count == 2
    assert final.last_review_id == history.last_review_id
    assert final.review_count == history.review_count
    assert final.history_hash == history.history_hash


def test_rwkv_checkpoint_prefixes_are_built_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(5)
    hash_review = rwkv_scheduler._rwkv_history_hash_after_review
    hashed_review_ids: list[int] = []

    def count_hashes(
        previous_hash: str,
        review_id: int,
        review: RwkvReviewInput,
    ) -> str:
        hashed_review_ids.append(review_id)
        return hash_review(previous_hash, review_id, review)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_history_hash_after_review",
        count_hashes,
    )

    prefixes = rwkv_scheduler._rwkv_history_prefixes(history, [2, 4, 2])

    assert hashed_review_ids == history.review_ids[:4]
    assert list(prefixes) == [2, 4]
    _assert_rwkv_checkpoint_history_matches(
        prefixes[2],
        _rwkv_checkpoint_test_history(2),
    )
    _assert_rwkv_checkpoint_history_matches(
        prefixes[4],
        _rwkv_checkpoint_test_history(4),
    )


def test_rwkv_checkpoint_suffix_prefixes_are_built_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(5)
    base = rwkv_scheduler._rwkv_history_prefix(history, 2)
    suffix = rwkv_scheduler._rwkv_validated_history_suffix(history, base)
    hash_review = rwkv_scheduler._rwkv_history_hash_after_review
    hashed_review_ids: list[int] = []

    def count_hashes(
        previous_hash: str,
        review_id: int,
        review: RwkvReviewInput,
    ) -> str:
        hashed_review_ids.append(review_id)
        return hash_review(previous_hash, review_id, review)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_history_hash_after_review",
        count_hashes,
    )

    prefixes = rwkv_scheduler._rwkv_history_with_suffix_prefixes(
        base,
        suffix,
        [1, 3],
    )

    assert hashed_review_ids == history.review_ids[2:]
    _assert_rwkv_checkpoint_history_matches(
        prefixes[1],
        _rwkv_checkpoint_test_history(3),
    )
    _assert_rwkv_checkpoint_history_matches(prefixes[3], history)


def test_rwkv_checkpoint_writer_builds_histories_on_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(5)
    hash_review = rwkv_scheduler._rwkv_history_hash_after_review
    hashed_review_ids: list[int] = []
    written_histories: list[RwkvHistoricalReviewInputs] = []
    history_map_ids: list[tuple[int, int, int]] = []

    def count_hashes(
        previous_hash: str,
        review_id: int,
        review: RwkvReviewInput,
    ) -> str:
        hashed_review_ids.append(review_id)
        return hash_review(previous_hash, review_id, review)

    def write_checkpoint(
        reviewer: object,
        context: object,
        checkpoint_history: RwkvHistoricalReviewInputs,
        snapshot: RwkvBackendCacheSnapshot,
    ) -> rwkv_scheduler._RwkvStateCacheCheckpointEntry:
        history_map_ids.append(
            (
                id(checkpoint_history.previous_review_id_by_card),
                id(checkpoint_history.previous_interval_days_by_card),
                id(checkpoint_history.review_count_by_card),
            )
        )
        written_histories.append(
            replace(
                checkpoint_history,
                previous_review_id_by_card=dict(
                    checkpoint_history.previous_review_id_by_card
                ),
                previous_interval_days_by_card=dict(
                    checkpoint_history.previous_interval_days_by_card
                ),
                review_count_by_card=dict(checkpoint_history.review_count_by_card),
            )
        )
        return rwkv_scheduler._rwkv_state_cache_checkpoint_entry(checkpoint_history)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_history_hash_after_review",
        count_hashes,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_write_context",
        lambda reviewer: object(),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_write_rwkv_state_cache_checkpoint",
        write_checkpoint,
    )
    writer = rwkv_scheduler._RwkvStateCacheCheckpointWriter(
        SimpleNamespace(),
        history,
        [2, 4],
    )
    snapshot = RwkvBackendCacheSnapshot({}, {}, {}, {}, None, None)

    assert hashed_review_ids == []
    writer(2, snapshot)
    assert hashed_review_ids == history.review_ids[:2]
    writer(4, snapshot)

    assert hashed_review_ids == history.review_ids[:4]
    assert history_map_ids[0] == history_map_ids[1]
    _assert_rwkv_checkpoint_history_matches(
        written_histories[0],
        _rwkv_checkpoint_test_history(2),
    )
    _assert_rwkv_checkpoint_history_matches(
        written_histories[1],
        _rwkv_checkpoint_test_history(4),
    )


def test_rwkv_checkpoint_writer_extends_base_history_on_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(5)
    base = rwkv_scheduler._rwkv_history_prefix(history, 2)
    suffix = rwkv_scheduler._rwkv_validated_history_suffix(history, base)
    written_histories: list[RwkvHistoricalReviewInputs] = []

    def write_checkpoint(
        reviewer: object,
        context: object,
        checkpoint_history: RwkvHistoricalReviewInputs,
        snapshot: RwkvBackendCacheSnapshot,
    ) -> rwkv_scheduler._RwkvStateCacheCheckpointEntry:
        written_histories.append(
            replace(
                checkpoint_history,
                previous_review_id_by_card=dict(
                    checkpoint_history.previous_review_id_by_card
                ),
                previous_interval_days_by_card=dict(
                    checkpoint_history.previous_interval_days_by_card
                ),
                review_count_by_card=dict(checkpoint_history.review_count_by_card),
            )
        )
        return rwkv_scheduler._rwkv_state_cache_checkpoint_entry(checkpoint_history)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_write_context",
        lambda reviewer: object(),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_write_rwkv_state_cache_checkpoint",
        write_checkpoint,
    )
    writer = rwkv_scheduler._RwkvStateCacheCheckpointWriter(
        SimpleNamespace(),
        suffix,
        [1, 3],
        base_history=base,
    )
    snapshot = RwkvBackendCacheSnapshot({}, {}, {}, {}, None, None)

    writer(1, snapshot)
    writer(3, snapshot)

    _assert_rwkv_checkpoint_history_matches(
        written_histories[0],
        _rwkv_checkpoint_test_history(3),
    )
    _assert_rwkv_checkpoint_history_matches(written_histories[1], history)


def test_rwkv_checkpoint_writer_without_endpoints_does_not_copy_base_history() -> None:
    history = _rwkv_checkpoint_test_history(3)
    writer = rwkv_scheduler._RwkvStateCacheCheckpointWriter(
        SimpleNamespace(),
        history,
        [],
        base_history=replace(history, replay_key="different"),
    )

    assert writer.context is None
    assert writer.entries == []


def test_rwkv_history_prefix_identities_are_hashed_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(5)
    hash_review = rwkv_scheduler._rwkv_history_hash_after_review
    hashed_review_ids: list[int] = []

    def count_hashes(
        previous_hash: str,
        review_id: int,
        review: RwkvReviewInput,
    ) -> str:
        hashed_review_ids.append(review_id)
        return hash_review(previous_hash, review_id, review)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_history_hash_after_review",
        count_hashes,
    )

    identities = rwkv_scheduler._rwkv_history_prefix_identities(
        history,
        [history.review_ids[0], history.review_ids[2], history.review_ids[-1]],
    )

    assert hashed_review_ids == history.review_ids[:3]
    assert identities[history.review_ids[0]].review_count == 1
    assert identities[history.review_ids[2]].review_count == 3
    assert identities[history.review_ids[-1]] == (
        history.last_review_id,
        history.review_count,
        history.history_hash,
    )


def test_rwkv_state_cache_save_computes_shared_metadata_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history = _rwkv_checkpoint_test_history(3)
    checkpoint_histories = rwkv_scheduler._rwkv_history_prefixes(
        history,
        [1, 2],
    )
    snapshot = RwkvBackendCacheSnapshot(
        card_states={},
        note_states={},
        deck_states={},
        preset_states={},
        global_state=None,
        runtime_state=b"runtime",
    )

    class Backend:
        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            return snapshot

    calls = {"collection": 0, "model": 0, "dynamic": 0}

    def collection_key(_reviewer: object) -> dict[str, object]:
        calls["collection"] += 1
        return {"collection": "test"}

    def model_key() -> dict[str, object]:
        calls["model"] += 1
        return {"model": "test"}

    def dynamic_replay(_reviewer: object) -> bool:
        calls["dynamic"] += 1
        return False

    set_reviewer_backend(cast(Any, Backend()))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_cache_key",
        collection_key,
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_key", model_key)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        dynamic_replay,
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    checkpoint_writer = rwkv_scheduler._RwkvStateCacheCheckpointWriter(
        reviewer,
        history,
        [1, 2],
    )
    for review_count in (1, 2):
        checkpoint_writer(
            review_count,
            snapshot,
        )
    cache_dir = tmp_path / "rwkv-state-cache"
    assert all(
        (
            cache_dir
            / f"checkpoint-v1-{checkpoint_histories[review_count].last_review_id}.bin"
        ).exists()
        for review_count in (1, 2)
    )
    for filename in rwkv_scheduler._RWKV_STATE_CACHE_LEGACY_DATA_FILES:
        (cache_dir / filename).write_bytes(b"legacy")

    rwkv_scheduler._save_reviewer_backend_cache(
        reviewer,
        history,
        checkpoint_entries=checkpoint_writer.entries,
        write_context=checkpoint_writer.context,
    )

    assert calls == {"collection": 1, "model": 1, "dynamic": 1}
    assert (cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_META_FILE).exists()
    assert all(
        not (cache_dir / filename).exists()
        for filename in rwkv_scheduler._RWKV_STATE_CACHE_LEGACY_DATA_FILES
    )


@pytest.mark.parametrize(
    ("recovered", "current_model", "segment_requested"),
    [
        # the everyday start-up: the new reviews go to the deltas log and the
        # snapshot segment stays, so a segment of the replay would be read by
        # nothing
        (False, True, False),
        # these saves write the replayed state and use what is written
        (True, True, True),
        (False, False, True),
    ],
)
def test_the_startup_replay_writes_a_segment_only_for_a_save_that_uses_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recovered: bool,
    current_model: bool,
    segment_requested: bool,
) -> None:
    """Each start-up after a day of reviews used to write a new ~58 MB segment
    of the store that the metadata never pointed to; only a full save pruned
    it. Measured on a copy of a 656,441-review collection: segments 3 and 4
    written with parent 2, snapshot segment still 2."""
    history = _rwkv_checkpoint_test_history(2)
    saved = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card={},
        previous_interval_days_by_card={},
        review_count_by_card={},
        last_review_id=0,
        review_count=0,
        history_hash=history.history_hash,
        replay_key=history.replay_key,
    )
    stored = rwkv_scheduler.RwkvStoredStateCache(
        metadata={},
        snapshot=None,
        history=saved,
        pending_history=history,
        recovered_from_checkpoint=recovered,
        state_store_path=tmp_path / "state.sqlite3",
        state_store_generation="generation",
        state_store_segment_id=2,
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache", lambda *_a, **_k: stored
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_uses_current_model_key",
        lambda _metadata: current_model,
    )
    requested: list[list[int]] = []

    def warm_up_reviews(*_args: object, **kwargs: object) -> None:
        requested.append(list(cast(Sequence[int], kwargs["snapshot_after_reviews"])))

    monkeypatch.setattr(rwkv_scheduler, "_warm_up_rwkv_reviews", warm_up_reviews)
    saves: list[str] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_append_rwkv_state_cache_deltas",
        lambda *_a, **_k: saves.append("append"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_save_reviewer_backend_cache",
        lambda *_a, **_k: saves.append("save"),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_refresh_rwkv_state_cache_collection_mod", lambda *_a: None
    )
    backend = SimpleNamespace(
        restore_cache_snapshot=lambda _snapshot: None,
        restore_state_cache_checkpoint=lambda *_args: None,
        warm_up=lambda _reviews: None,
    )

    rwkv_scheduler._restore_reviewer_backend_cache(
        SimpleNamespace(),
        backend=cast(Any, backend),
        is_current=lambda: True,
    )

    assert requested == [[len(history.reviews)] if segment_requested else []]
    assert saves == ["append" if not recovered and current_model else "save"]


def test_rwkv_delta_store_full_checkpoint_starts_new_parent_chain(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "rwkv-state-cache"
    cache_dir.mkdir()
    store_path = cache_dir / "state.sqlite3"
    store_path.write_bytes(b"existing-store")
    history = _rwkv_checkpoint_test_history(3)
    checkpoint_histories = rwkv_scheduler._rwkv_history_prefixes(history, [1, 3])
    calls: list[tuple[Path, str, int | None, int, bool]] = []

    def write_checkpoint(
        path: Path,
        generation: str,
        parent_segment_id: int | None,
        last_review_id: int,
        _review_count: int,
        _history_hash: str,
        _replay_key: str,
        _previous_review_ids: bytes,
        _previous_intervals: bytes,
        _review_counts: bytes,
        full: bool,
        durable: bool,
    ) -> int:
        assert durable is True
        calls.append((path, generation, parent_segment_id, last_review_id, full))
        with path.open("ab") as file:
            file.write(b"segment")
        return len(calls)

    checkpoint_writer = rwkv_scheduler._RwkvStateCacheCheckpointWriter(
        _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[]),
        history,
        [1, 3],
        full_review_counts=[1],
        state_store_path=store_path,
        state_store_generation="generation",
        parent_segment_id=99,
    )
    checkpoint_writer.write_runtime_checkpoint(
        1,
        write_checkpoint,
    )
    checkpoint_writer.write_runtime_checkpoint(
        3,
        write_checkpoint,
    )

    assert calls[0][2:] == (
        None,
        checkpoint_histories[1].last_review_id,
        True,
    )
    assert calls[1][2:] == (
        1,
        checkpoint_histories[3].last_review_id,
        False,
    )
    assert calls[0][0] == calls[1][0]
    assert calls[0][1] == calls[1][1]
    assert [entry["segmentId"] for entry in checkpoint_writer.entries] == [1, 2]
    assert checkpoint_writer.context is not None
    assert checkpoint_writer.context.state_store_head_segment_id == 2


def test_rwkv_delta_store_reuses_final_checkpoint_as_snapshot(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "rwkv-state-cache"
    cache_dir.mkdir()
    temporary_store = cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_STORE_TEMP_FILE
    temporary_store.write_bytes(b"delta-store")
    history = _rwkv_checkpoint_test_history(3)
    checkpoint_history = rwkv_scheduler._rwkv_history_prefix(history, 1)
    context = rwkv_scheduler._RwkvStateCacheWriteContext(
        cache_dir=cache_dir,
        metadata_base={
            "version": rwkv_scheduler._RWKV_STATE_CACHE_VERSION,
            "presetReplaySemantics": (
                rwkv_scheduler._RWKV_PRESET_REPLAY_SEMANTICS_VERSION
            ),
            "collection": {"test": True},
            "model": {"test": True},
            "dynamicPresetReplay": False,
        },
        state_store_path=temporary_store,
        state_store_generation="generation",
        state_store_temporary=True,
        state_store_head_segment_id=2,
    )

    finished_checkpoint_writes = False

    class Backend:
        def write_state_cache_checkpoint(
            self, *_args: object, **_kwargs: object
        ) -> int:
            raise AssertionError("the final checkpoint must be reused")

        def finish_state_cache_checkpoints(self) -> None:
            nonlocal finished_checkpoint_writes
            finished_checkpoint_writes = True

    checkpoint_entry = rwkv_scheduler._rwkv_state_cache_checkpoint_entry(
        checkpoint_history,
        segment_id=1,
    )
    final_entry = rwkv_scheduler._rwkv_state_cache_checkpoint_entry(
        history,
        segment_id=2,
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])

    rwkv_scheduler._save_reviewer_backend_state_store(
        reviewer,
        history,
        backend=cast(Any, Backend()),
        checkpoint_entries=[checkpoint_entry, final_entry],
        context=context,
    )

    live_store = cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_STORE_FILE
    assert finished_checkpoint_writes
    assert live_store.read_bytes() == b"delta-store"
    assert not (cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_SNAPSHOT_FILE).exists()
    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert metadata["storage"] == rwkv_scheduler._RWKV_STATE_CACHE_STORE_KIND
    assert metadata["snapshotSegmentId"] == 2
    assert metadata["checkpoints"] == [checkpoint_entry]


def test_rwkv_delta_store_prune_removes_unreachable_entity_states(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(store_path) as connection:
        connection.executescript(
            f"""
            pragma user_version = {rwkv_scheduler._RWKV_STATE_CACHE_STORE_SCHEMA_VERSION};
            create table store_metadata (
              key text primary key,
              value text not null
            );
            create table segments (
              id integer primary key,
              parent_id integer
            );
            create table entity_states (
              id integer primary key,
              segment_id integer not null,
              kind integer not null,
              entity_id integer not null,
              state blob
            );
            insert into store_metadata values ('generation', 'generation');
            insert into segments values (1, null), (2, 1), (3, null);
            insert into entity_states values
              (1, 1, 0, 10, X'01'),
              (2, 2, 0, 11, X'02'),
              (3, 3, 0, 12, X'03');
            """
        )

    rwkv_scheduler._prune_rwkv_state_cache_store(
        store_path,
        "generation",
        2,
    )

    with sqlite3.connect(store_path) as connection:
        assert connection.execute("select id from segments order by id").fetchall() == [
            (1,),
            (2,),
        ]
        assert connection.execute(
            "select segment_id from entity_states order by segment_id"
        ).fetchall() == [(1,), (2,)]


def test_rwkv_state_cache_connection_always_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, path: Path) -> None:
            self.connection = real_connect(path)
            self.closed = False

        def __enter__(self) -> TrackingConnection:
            self.connection.__enter__()
            return self

        def __exit__(self, *args: object) -> bool:
            return bool(self.connection.__exit__(*args))

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

        def close(self) -> None:
            self.closed = True
            self.connection.close()

    connections: list[TrackingConnection] = []

    def connect(path: Path) -> TrackingConnection:
        connection = TrackingConnection(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(rwkv_scheduler.sqlite3, "connect", connect)
    store_path = tmp_path / "state.sqlite3"

    with rwkv_scheduler._rwkv_state_cache_connection(store_path) as connection:
        connection.execute("create table test (value integer)")
    assert connections[-1].closed

    with pytest.raises(RuntimeError):
        with rwkv_scheduler._rwkv_state_cache_connection(store_path):
            raise RuntimeError
    assert connections[-1].closed


def test_unchanged_rwkv_delta_store_reads_effective_segment_without_history_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history = _rwkv_checkpoint_test_history(3)
    cache_dir = tmp_path / "rwkv-state-cache"
    cache_dir.mkdir()
    (cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_DELTAS_FILE).write_bytes(
        rwkv_scheduler._rwkv_empty_deltas_log()
    )
    metadata: dict[str, object] = {
        "storage": rwkv_scheduler._RWKV_STATE_CACHE_STORE_KIND,
        "storeGeneration": "generation",
        "snapshotSegmentId": 2,
        "snapshotReviewId": history.last_review_id,
        "snapshotHistoryHash": history.history_hash,
        "lastReviewId": history.last_review_id,
        "reviewCount": history.review_count,
        "historyHash": history.history_hash,
        "replayKey": history.replay_key,
    }
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_rwkv_state_cache_store_segment_history",
        lambda _path, _generation, _segment_id: replace(
            history,
            reviews=[],
            review_ids=[],
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_store_segment_chain",
        lambda _path, _generation, _segment_id: [2],
    )

    stored = rwkv_scheduler._read_unchanged_rwkv_state_cache_store(
        backend=cast(
            Any,
            SimpleNamespace(restore_state_cache_checkpoint=lambda *_args: None),
        ),
        cache_dir=cache_dir,
        metadata=metadata,
    )

    assert stored is not None
    assert stored.history.last_review_id == history.last_review_id
    assert stored.history.history_hash == history.history_hash
    assert stored.pending_history is not None
    assert stored.pending_history.reviews == []
    assert stored.pending_history.review_ids == []
    assert stored.state_store_segment_id == 2


def test_rwkv_delta_store_recovers_when_manifest_is_ahead_of_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_history = _rwkv_checkpoint_test_history(3)
    snapshot_history = rwkv_scheduler._rwkv_history_prefix(current_history, 2)
    metadata: dict[str, object] = {
        "storeGeneration": "generation",
        "snapshotSegmentId": 2,
        "snapshotReviewId": current_history.last_review_id,
        "snapshotHistoryHash": current_history.history_hash,
        "lastReviewId": current_history.last_review_id,
        "reviewCount": current_history.review_count,
        "historyHash": current_history.history_hash,
        "checkpoints": [],
    }

    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_rwkv_state_cache_store_segment_history",
        lambda _path, _generation, segment_id: (
            snapshot_history
            if segment_id == 2
            else pytest.fail(f"unexpected segment: {segment_id}")
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_store_segment_chain",
        lambda _path, _generation, _segment_id: [2],
    )

    stored = rwkv_scheduler._read_rwkv_state_cache_store(
        _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[]),
        backend=cast(
            Any,
            SimpleNamespace(restore_state_cache_checkpoint=lambda *_args: None),
        ),
        cache_dir=tmp_path / "rwkv-state-cache",
        metadata=metadata,
        current_history=current_history,
        desired_checkpoint_review_counts=(),
        dynamic_preset_replay_enabled=False,
    )

    assert stored is not None
    assert stored.recovered_from_checkpoint is True
    assert stored.state_store_segment_id == 2
    _assert_rwkv_checkpoint_history_matches(stored.history, snapshot_history)
    assert stored.pending_history is not None
    assert stored.pending_history.review_ids == current_history.review_ids[2:]


def test_rwkv_state_cache_file_replace_retries_windows_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "building.sqlite3"
    destination = tmp_path / "state.sqlite3"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    original_replace = os.replace
    attempts = 0
    delays: list[float] = []

    def replace_after_transient_locks(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated Windows sharing violation")
        original_replace(source, destination)

    monkeypatch.setattr(rwkv_scheduler.sys, "platform", "win32")
    monkeypatch.setattr(rwkv_scheduler.os, "replace", replace_after_transient_locks)
    monkeypatch.setattr(rwkv_scheduler.time, "sleep", delays.append)

    rwkv_scheduler._replace_rwkv_state_cache_file(source, destination)

    assert attempts == 3
    assert delays == list(rwkv_scheduler._RWKV_STATE_CACHE_REPLACE_RETRY_DELAYS[:2])
    assert destination.read_bytes() == b"new"
    assert not source.exists()


@pytest.mark.parametrize("value", [None, 0, -1, -(2**63), 2**63 - 1])
def test_rwkv_optional_integer_preserves_cache_bytes(value: int | None) -> None:
    expected = b"\0" if value is None else b"\1" + struct.pack("<q", value)
    buffer = bytearray()
    stream = io.BytesIO()
    for output in (buffer, stream):
        rwkv_scheduler._write_optional_i64(output, value)
    assert bytes(buffer) == stream.getvalue() == expected
    reader = rwkv_scheduler._RwkvBinaryReader(expected)
    assert rwkv_scheduler._read_optional_i64(reader) == value
    reader.expect_end()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, b"\0"), ("", b"\1\0\0\0\0"), ("é", b"\1\2\0\0\0\xc3\xa9")],
)
def test_rwkv_optional_string_preserves_cache_bytes(
    value: str | None, expected: bytes
) -> None:
    buffer = bytearray()
    stream = io.BytesIO()
    for output in (buffer, stream):
        rwkv_scheduler._write_optional_string(output, value)
    assert bytes(buffer) == stream.getvalue() == expected
    reader = rwkv_scheduler._RwkvBinaryReader(expected)
    assert rwkv_scheduler._read_optional_string(reader) == value
    reader.expect_end()


def test_rwkv_state_cache_stream_write_matches_binary_encoder(tmp_path: Path) -> None:
    history = _rwkv_checkpoint_test_history(2)
    snapshot = RwkvBackendCacheSnapshot(
        card_states={1: b"card"},
        note_states={10: b"note"},
        deck_states={100: b"deck"},
        preset_states={1000: b"preset"},
        global_state=b"global",
        runtime_state=b"runtime",
    )
    metadata = {
        "lastReviewId": history.last_review_id,
        "reviewCount": history.review_count,
        "historyHash": history.history_hash,
        "replayKey": history.replay_key,
    }
    path = tmp_path / "snapshot.bin"

    rwkv_scheduler._atomic_write_rwkv_state_cache_snapshot(
        path,
        metadata=metadata,
        snapshot=snapshot,
        history=history,
    )

    assert path.read_bytes() == rwkv_scheduler._encode_rwkv_state_cache_snapshot_file(
        metadata=metadata,
        snapshot=snapshot,
        history=history,
    )
    assert rwkv_scheduler._validate_rwkv_state_cache_snapshot_file(path) == metadata
    decoded_metadata, decoded_snapshot, decoded_history = (
        rwkv_scheduler._read_rwkv_state_cache_snapshot_file(path)
    )
    assert decoded_metadata == metadata
    assert decoded_snapshot == snapshot
    _assert_rwkv_checkpoint_history_matches(decoded_history, history)


def test_rwkv_state_cache_runtime_stream_matches_snapshot_writer(
    tmp_path: Path,
) -> None:
    history = _rwkv_checkpoint_test_history(2)
    snapshot = RwkvBackendCacheSnapshot(
        card_states={1: b"card"},
        note_states={10: b"note"},
        deck_states={100: b"deck"},
        preset_states={1000: b"preset"},
        global_state=b"global",
        runtime_state=b"runtime",
    )
    metadata = {
        "lastReviewId": history.last_review_id,
        "reviewCount": history.review_count,
        "historyHash": history.history_hash,
        "replayKey": history.replay_key,
    }
    expected_path = tmp_path / "expected.bin"
    streamed_path = tmp_path / "streamed.bin"

    def append_snapshot(path: Path) -> None:
        with path.open("ab") as file:
            rwkv_scheduler._write_cache_snapshot_binary(file, snapshot)

    rwkv_scheduler._atomic_write_rwkv_state_cache_snapshot(
        expected_path,
        metadata=metadata,
        snapshot=snapshot,
        history=history,
    )
    rwkv_scheduler._atomic_write_rwkv_state_cache_snapshot_from_runtime(
        streamed_path,
        metadata=metadata,
        append_snapshot=append_snapshot,
        history=history,
    )

    assert streamed_path.read_bytes() == expected_path.read_bytes()


def test_rwkv_state_cache_save_prefers_resident_runtime_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history = _rwkv_checkpoint_test_history(2)
    snapshot = RwkvBackendCacheSnapshot(
        card_states={1: b"card"},
        note_states={10: b"note"},
        deck_states={100: b"deck"},
        preset_states={1000: b"preset"},
        global_state=b"global",
        runtime_state=b"runtime",
    )

    class Backend:
        def __init__(self) -> None:
            self.stream_calls = 0

        def supports_streaming_cache_snapshot(self) -> bool:
            return True

        def append_cache_snapshot_binary(self, path: Path) -> None:
            self.stream_calls += 1
            with path.open("ab") as file:
                rwkv_scheduler._write_cache_snapshot_binary(file, snapshot)

        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            raise AssertionError("streaming save must not build a Python snapshot")

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    backend = Backend()
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])

    rwkv_scheduler._save_reviewer_backend_cache(
        reviewer,
        history,
        backend=cast(Any, backend),
    )

    assert backend.stream_calls == 1
    _, decoded_snapshot, decoded_history = (
        rwkv_scheduler._read_rwkv_state_cache_snapshot_file(
            tmp_path
            / "rwkv-state-cache"
            / rwkv_scheduler._RWKV_STATE_CACHE_SNAPSHOT_FILE
        )
    )
    assert decoded_snapshot == snapshot
    _assert_rwkv_checkpoint_history_matches(decoded_history, history)


def test_reviewer_rwkv_warmup_saves_and_reuses_local_state_cache(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert (tmp_path / "rwkv-state-cache" / "snapshot-v1.bin").exists()
    assert (tmp_path / "rwkv-state-cache" / "deltas-v1.log").exists()
    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert metadata.get("checkpoints", []) == []
    assert (
        tmp_path / "rwkv-state-cache" / f"checkpoint-v1-{first_review}.bin"
    ).exists() is False

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert restored_runtime.reviewed == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    snapshot = rwkv_scheduler._reviewer_backend.cache_snapshot()
    assert snapshot.card_states[1] == b"card-1-3"
    assert snapshot.global_state == b"global-2"


def test_reviewer_rwkv_cache_skips_history_scan_when_collection_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        ((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500),
        ((41 * 86_400 + 3_700) * 1000, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_modified",
        lambda _reviewer: 12345,
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda *_args, **_kwargs: pytest.fail(
            "unchanged collection should not rebuild canonical history"
        ),
    )

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert restored_runtime.reviewed == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]


def test_rwkv_historical_fingerprint_passes_stable_addon_preset_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Backend:
        def rwkv_historical_review_fingerprint(
            self,
            **kwargs: object,
        ) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                last_review_id=2_000,
                review_count=2,
                history_hash="a" * 64,
                active_ignored_review_ids=[1_000],
                queried_review_count=2,
                history_is_valid=True,
            )

    overlay = {
        "presets": [{"id": "addon:current"}],
        "rules": [{"preset_id": "addon:rule"}],
        "simulator_rules": [{"preset_id": "addon:simulator"}],
    }
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Backend(),
                get_config=lambda _key: overlay,
                decks=SimpleNamespace(
                    all_config=lambda: [
                        {
                            "id": 123,
                            "rwkvReviewFirstReviewElapsedFromCardCreation": False,
                        },
                        {
                            "id": 456,
                            "rwkvReviewFirstReviewElapsedFromCardCreation": True,
                        },
                    ]
                ),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda _reviewer: True,
    )

    fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
        reviewer,
        ignored_review_ids=(1_000, 3_000),
        expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=2_000,
            review_count=2,
            history_hash="a" * 64,
        ),
    )

    assert fingerprint == rwkv_scheduler._RwkvHistoricalReviewFingerprint(
        identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=2_000,
            review_count=2,
            history_hash="a" * 64,
        ),
        active_ignored_review_ids=(1_000,),
        queried_review_count=2,
        history_is_valid=True,
    )
    assert calls == [
        {
            "ignored_review_ids": (1_000, 3_000),
            "dynamic_preset_replay": True,
            "stable_preset_ids": {
                preset_id: _expected_preset_hash(preset_id)
                for preset_id in (
                    "addon:current",
                    "addon:rule",
                    "addon:simulator",
                )
            },
            # a stored off value is ignored (spec deck-options.rwkv-fixed-settings)
            "first_review_uses_creation_by_config_id": {123: True, 456: True},
            "expected_identity": scheduler_pb2.RwkvHistoricalReviewIdentity(
                last_review_id=2_000,
                review_count=2,
                history_hash="a" * 64,
            ),
        }
    ]


def test_rwkv_state_cache_uses_matching_rust_history_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = cast(Any, object())
    validation_calls: list[dict[str, object]] = []
    identity = rwkv_scheduler._RwkvHistoryPrefixIdentity(
        last_review_id=2_000,
        review_count=2,
        history_hash="b" * 64,
    )

    def validate_history(
        *_args: object,
        **kwargs: object,
    ) -> rwkv_scheduler._RwkvHistoricalReviewFingerprint:
        validation_calls.append(kwargs)
        return rwkv_scheduler._RwkvHistoricalReviewFingerprint(
            identity=identity,
            active_ignored_review_ids=(1_000,),
            queried_review_count=2,
            history_is_valid=True,
        )

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_historical_review_fingerprint",
        validate_history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda *_args, **_kwargs: "replay-key",
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_unchanged_rwkv_state_cache_binary",
        lambda *_args, **_kwargs: expected,
    )

    stored = rwkv_scheduler._read_rwkv_state_cache_from_rust_fingerprint(
        SimpleNamespace(),
        backend=cast(Any, object()),
        cache_dir=tmp_path,
        metadata={
            "lastReviewId": identity.last_review_id,
            "reviewCount": identity.review_count,
            "historyHash": identity.history_hash,
            "replayKey": "replay-key",
        },
        ignored_review_ids=(1_000,),
    )

    assert stored is expected
    assert validation_calls == [
        {
            "ignored_review_ids": (1_000,),
            "expected_identity": identity,
        }
    ]


def test_rwkv_state_cache_rejects_mismatched_rust_history_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_historical_review_fingerprint",
        lambda *_args, **_kwargs: rwkv_scheduler._RwkvHistoricalReviewFingerprint(
            identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
                last_review_id=2_000,
                review_count=2,
                history_hash="b" * 64,
            ),
            active_ignored_review_ids=(),
            queried_review_count=2,
            history_is_valid=False,
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda *_args, **_kwargs: "replay-key",
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_unchanged_rwkv_state_cache_binary",
        lambda *_args, **_kwargs: pytest.fail(
            "mismatched fingerprint must not restore the cache"
        ),
    )

    assert (
        rwkv_scheduler._read_rwkv_state_cache_from_rust_fingerprint(
            SimpleNamespace(),
            backend=cast(Any, object()),
            cache_dir=tmp_path,
            metadata={
                "lastReviewId": 2_000,
                "reviewCount": 2,
                "historyHash": "c" * 64,
                "replayKey": "replay-key",
            },
            ignored_review_ids=(),
        )
        is None
    )


def test_rwkv_state_cache_that_only_newer_reviews_follow_replays_just_those(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A day of reviews since the state was saved used to send every start-up
    through the rebuild of the whole history: 19.1 s on 656,421 reviews, for
    19 new ones, against 0.3 s when nothing was new. When the saved state is
    the start of the history, the cache is taken as it is and the restore
    reads only the reviews after it (`pending_history` None), the same
    incremental read a sync uses."""
    saved = rwkv_scheduler._RwkvHistoryPrefixIdentity(
        last_review_id=2_000,
        review_count=2,
        history_hash="a" * 64,
    )
    fingerprint_says: dict[str, bool] = {}
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_historical_review_fingerprint",
        lambda *_args, **_kwargs: rwkv_scheduler._RwkvHistoricalReviewFingerprint(
            identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
                last_review_id=4_000,
                review_count=4,
                history_hash="b" * 64,
            ),
            active_ignored_review_ids=(),
            queried_review_count=4,
            **fingerprint_says,  # type: ignore[arg-type]
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda *_args, **_kwargs: "replay-key",
    )
    saved_history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card={10: 2_000},
        previous_interval_days_by_card={10: 3},
        review_count_by_card={10: 2},
        last_review_id=2_000,
        review_count=2,
        history_hash="a" * 64,
        replay_key="replay-key",
    )
    unchanged = rwkv_scheduler.RwkvStoredStateCache(
        metadata={},
        snapshot=None,
        history=saved_history,
        pending_history=rwkv_scheduler._rwkv_empty_history_suffix(saved_history),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_unchanged_rwkv_state_cache_binary",
        lambda *_args, **_kwargs: unchanged,
    )

    def read() -> rwkv_scheduler.RwkvStoredStateCache | None:
        return rwkv_scheduler._read_rwkv_state_cache_from_rust_fingerprint(
            SimpleNamespace(),
            backend=cast(Any, object()),
            cache_dir=tmp_path,
            metadata={
                "lastReviewId": saved.last_review_id,
                "reviewCount": saved.review_count,
                "historyHash": saved.history_hash,
                "replayKey": "replay-key",
            },
            ignored_review_ids=(),
        )

    # the saved state is the start of the history: taken, and the reviews
    # after it are left for the restore to read
    fingerprint_says.update(history_is_valid=False, history_prefix_is_valid=True)
    prefix = read()
    assert prefix is not None
    assert prefix.pending_history is None
    assert prefix.history is saved_history
    # the per-card state the incremental read continues from is the saved one
    assert prefix.history.previous_review_id_by_card == {10: 2_000}
    assert prefix.history.review_count_by_card == {10: 2}

    # nothing new: the empty suffix stands, and nothing is read
    fingerprint_says.update(history_is_valid=True, history_prefix_is_valid=True)
    assert read() is unchanged

    # neither: the history no longer starts with the saved state
    fingerprint_says.update(history_is_valid=False, history_prefix_is_valid=False)
    assert read() is None


def test_the_restore_reads_only_the_reviews_after_a_saved_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `pending_history` None the restore reads the reviews after the
    saved state's last one, handing over the saved per-card state; it never
    asks for the whole history."""
    saved_history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card={10: 2_000},
        previous_interval_days_by_card={10: 3},
        review_count_by_card={10: 2},
        last_review_id=2_000,
        review_count=2,
        history_hash="a" * 64,
        replay_key="replay-key",
    )
    stored = rwkv_scheduler.RwkvStoredStateCache(
        metadata={},
        snapshot=cast(Any, object()),
        history=saved_history,
        pending_history=None,
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache", lambda *_args, **_kwargs: stored
    )
    reads: list[dict[str, object]] = []

    def inputs(_reviewer: object, **kwargs: object) -> object:
        reads.append(kwargs)
        # stop the restore here: what it asked for is the point
        raise rwkv_scheduler._ReviewerBackendWarmupInvalidated

    monkeypatch.setattr(rwkv_scheduler, "_historical_rwkv_review_inputs", inputs)
    backend = SimpleNamespace(
        restore_cache_snapshot=lambda _snapshot: None,
        warm_up=lambda _reviews: None,
    )

    with pytest.raises(rwkv_scheduler._ReviewerBackendWarmupInvalidated):
        rwkv_scheduler._restore_reviewer_backend_cache(
            SimpleNamespace(),
            backend=cast(Any, backend),
            is_current=lambda: True,
        )

    assert len(reads) == 1
    assert reads[0]["after_review_id"] == 2_000
    assert reads[0]["previous_review_id_by_card"] == {10: 2_000}
    assert reads[0]["previous_interval_days_by_card"] == {10: 3}
    assert reads[0]["review_count_by_card"] == {10: 2}
    assert reads[0]["previous_history_hash"] == "a" * 64


def test_reviewer_rwkv_cache_adds_collection_marker_after_full_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500)]
    collection_mod: list[int | None] = [None]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_modified",
        lambda _reviewer: collection_mod[0],
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert "collectionMod" not in metadata

    collection_mod[0] = 12345
    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert metadata["collectionMod"] == 12345


def test_reviewer_rwkv_cache_refreshes_collection_marker_from_resident_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collection_mod = 2
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_modified",
        lambda _reviewer: collection_mod,
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    identity = _rwkv_resident_identity()
    cache_dir = tmp_path / "rwkv-state-cache"
    cache_dir.mkdir()
    rwkv_scheduler._atomic_write(
        cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_META_FILE,
        json.dumps(
            {
                "version": rwkv_scheduler._RWKV_STATE_CACHE_VERSION,
                "lastReviewId": identity.last_review_id,
                "reviewCount": identity.review_count,
                "historyHash": identity.history_hash,
                "replayKey": identity.replay_key,
                "collectionMod": 1,
            }
        ).encode("utf8"),
    )

    rwkv_scheduler._refresh_rwkv_state_cache_collection_mod(reviewer, identity)

    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert metadata["collectionMod"] == collection_mod


def test_reviewer_rwkv_cache_rebuilds_when_cached_prefix_contents_change(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    collection_mod = [12345]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_modified",
        lambda _reviewer: collection_mod[0],
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    rows[0] = (first_review, 1, 10, 100, 1, 1234, 0, 3, 2500)
    collection_mod[0] += 1
    rebuilt_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(rebuilt_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert rebuilt_runtime.reviewed == [(1, 1), (1, 3)]


# Pins spec/deck-options.md#deck-options.rwkv-fixed-settings
def test_reviewer_rwkv_cache_survives_a_stored_creation_elapsed_change(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    deck_config_overrides: dict[str, object] = {
        "rwkvReviewFirstReviewElapsedFromCardCreation": False,
    }
    rows = [(first_review, card_id, 10, 100, 2, 1234, 0, 3, 2500)]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(
        profile_folder=tmp_path,
        rows=rows,
        deck_config_overrides=deck_config_overrides,
    )
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    deck_config_overrides["rwkvReviewFirstReviewElapsedFromCardCreation"] = True
    rebuilt_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(rebuilt_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    # the stored value is ignored, so the replay semantics are the same and
    # the saved state is restored instead of replayed
    assert rebuilt_runtime.reviewed == []


def test_historical_rwkv_review_inputs_keeps_collection_scope_for_count(
    monkeypatch,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 0, 3, 2500, 1),
        (second_review, 2, 20, 200, 3, 2345, 0, 5, 2400, 1),
    ]
    count_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_rows",
        lambda reviewer, *, after_review_id=None, deck_id=None, between_parts=None: (
            rows
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_deck_config_ids_by_card",
        lambda reviewer, rows, deck_configs_by_deck_id=None: {1: 1000, 2: 2000},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolved_fsrs_preset_ids",
        lambda reviewer, card_ids: {},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda reviewer, deck_id: {"id": deck_id * 10},
    )

    def count_through(
        reviewer: object,
        last_review_id: int,
        *,
        deck_id: int | None = None,
    ) -> int:
        count_calls.append((last_review_id, deck_id))
        return 2

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_count_through",
        count_through,
    )

    history = rwkv_scheduler._historical_rwkv_review_inputs(SimpleNamespace())

    assert count_calls == []
    assert history.deck_id is None
    assert history.review_count == 2
    assert [review.identity.preset_id for review in history.reviews] == [1000, 2000]


def test_historical_rwkv_review_inputs_skips_full_scan_when_cache_is_current(
    monkeypatch,
) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            queries.append((sql, args))
            return []

    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: pytest.fail("unchanged history should return before timing"),
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    history = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        after_review_id=1234,
        previous_review_id_by_card={1: 1234},
        previous_interval_days_by_card={1: 10},
        review_count_by_card={1: 3, 2: 2},
    )

    assert len(queries) == 1
    sql, args = queries[0]
    assert "e.id > ?" in sql
    assert "limit 1" in sql
    assert args == (1234,)
    assert history.reviews == []
    assert history.review_ids == []
    assert history.last_review_id == 1234
    assert history.review_count == 5


def test_historical_rwkv_review_rows_do_not_repeat_note_payloads() -> None:
    captured_sql: list[str] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            captured_sql.append(sql)
            return []

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert rwkv_scheduler._historical_rwkv_review_rows(reviewer) == []

    sql = captured_sql[0].lower()
    assert "join cards" in sql
    assert "join notes" not in sql
    assert "n.tags" not in sql
    assert "n.flds" not in sql
    assert "case when c.odid != 0 then c.odid else c.did end" in sql


def test_historical_rwkv_review_count_excludes_deleted_cards() -> None:
    captured_sql: list[str] = []

    class DB:
        def scalar(self, sql: str, *args: object) -> int:
            captured_sql.append(sql)
            assert args == (1234,)
            return 2

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert (
        rwkv_scheduler._historical_rwkv_review_count_through(
            reviewer,
            1234,
            deck_id=100,
        )
        == 2
    )

    sql = captured_sql[0].lower()
    assert "from revlog r" in sql
    assert "join cards c on c.id = r.cid" in sql
    assert "(case when c.odid != 0 then c.odid else c.did end) in (100)" in sql
    assert "e.id <= ?" in sql


def test_rwkv_state_cache_refuses_to_persist_deck_scoped_history(
    tmp_path,
    caplog,
) -> None:
    review_id = (40 * 86_400 + 100) * 1000
    reviewer = _rwkv_cache_reviewer(
        profile_folder=tmp_path,
        rows=[(review_id, 1, 10, 100, 2, 1234, 1, 3, 2500)],
    )
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer, deck_id=100)

    with caplog.at_level("WARNING", logger="aqt.rwkv_scheduler"):
        rwkv_scheduler._save_reviewer_backend_cache(reviewer, history)
        rwkv_scheduler._append_rwkv_state_cache_deltas(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
        )

    cache_dir = tmp_path / "rwkv-state-cache"
    assert not (cache_dir / "snapshot-v1.bin").exists()
    assert not (cache_dir / "deltas-v1.log").exists()
    assert (
        "refusing to save scoped RWKV state cache history: deck_id=100" in caplog.text
    )
    assert (
        "refusing to append deltas scoped RWKV state cache history: deck_id=100"
        in caplog.text
    )


def test_rwkv_delta_writer_batches_small_records(
    monkeypatch,
    tmp_path,
) -> None:
    class RecordingAppendFile:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.flushed = False

        def write(self, data: bytes | bytearray) -> int:
            self.writes.append(bytes(data))
            return len(data)

        def flush(self) -> None:
            self.flushed = True

        def fileno(self) -> int:
            return -1

        def __enter__(self) -> "RecordingAppendFile":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class RecordingAppendPath:
        def __init__(self) -> None:
            self.file = RecordingAppendFile()

        def exists(self) -> bool:
            return False

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=0)

        def open(self, mode: str) -> RecordingAppendFile:
            assert mode == "ab"
            return self.file

    fsynced: list[int] = []
    monkeypatch.setattr(
        rwkv_scheduler.os,
        "fsync",
        lambda fileno: fsynced.append(fileno),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_RWKV_STATE_CACHE_DELTA_WRITE_BUFFER_SIZE",
        1024 * 1024,
    )

    review_ids = [100, 200, 300]
    reviews = [
        _rwkv_review_input(card_id=card_id, note_id=card_id + 1000)
        for card_id in (1, 2, 3)
    ]
    path = RecordingAppendPath()

    rwkv_scheduler._append_rwkv_delta_records(cast(Any, path), review_ids, reviews)

    assert len(path.file.writes) == 1
    assert path.file.flushed
    assert fsynced == [-1]
    delta_path = tmp_path / "deltas-v1.log"
    delta_path.write_bytes(path.file.writes[0])
    assert rwkv_scheduler._read_rwkv_delta_records(
        delta_path,
        after_review_id=0,
        until_review_id=300,
    ) == list(zip(review_ids, reviews))


def test_reviewer_rwkv_warmup_preserves_rated_manual_review_state(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    manual_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (manual_review, 1, 10, 100, 1, 2345, 4, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert runtime.reviewed == [(1, 2), (1, 1)]
    manual_input = runtime.answered_inputs[1]
    assert manual_input.card_type == int(rwkv_scheduler.RwkvReviewState.MANUAL)
    assert manual_input.card_queue == int(rwkv_scheduler.QUEUE_TYPE_REV)
    assert manual_input.current_state_kind == "normal"
    assert manual_input.current_normal_state_kind == "review"
    assert manual_input.current_elapsed_seconds == 90_000
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == []


def test_rwkv_model_cache_key_uses_model_content_not_install_path(
    monkeypatch,
    tmp_path,
) -> None:
    first_model_path = (
        tmp_path / "first-install" / "rwkv_inference" / "RWKV_trained_on_5000_10000.bin"
    )
    second_model_path = (
        tmp_path
        / "second-install"
        / "rwkv_inference"
        / "RWKV_trained_on_5000_10000.bin"
    )
    first_model_path.parent.mkdir(parents=True)
    second_model_path.parent.mkdir(parents=True)
    first_model_path.write_bytes(b"same model")
    second_model_path.write_bytes(b"same model")

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: first_model_path,
    )
    first_key = rwkv_scheduler._rwkv_model_cache_key()
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: second_model_path,
    )
    second_key = rwkv_scheduler._rwkv_model_cache_key()

    assert first_key == second_key
    assert first_key is not None
    assert first_key["sha256"] == hashlib.sha256(b"same model").hexdigest()
    assert "path" not in first_key
    assert "mtimeNs" not in first_key


def test_rwkv_model_cache_key_changes_when_model_content_changes(
    monkeypatch,
    tmp_path,
) -> None:
    model_path = tmp_path / "RWKV_trained_on_5000_10000.bin"

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )

    model_path.write_bytes(b"model one")
    first_key = rwkv_scheduler._rwkv_model_cache_key()
    first_stat = model_path.stat()
    model_path.write_bytes(b"model two")
    # Windows may preserve timestamps across rapid same-size overwrites. Move
    # mtime forward explicitly so this tests cache invalidation instead of the
    # host filesystem's timestamp update behavior.
    os.utime(
        model_path,
        ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns + 1_000_000_000),
    )
    second_key = rwkv_scheduler._rwkv_model_cache_key()

    assert first_key is not None
    assert second_key is not None
    assert first_key["size"] == second_key["size"]
    assert first_key["sha256"] != second_key["sha256"]


def test_rwkv_model_cache_key_memoizes_and_detects_replaced_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "RWKV_trained_on_5000_10000.bin"
    model_path.write_bytes(b"model one")
    original_stat = model_path.stat()
    hash_file = rwkv_scheduler._sha256_file
    hashed_paths: list[Path] = []

    def count_hash(path: Path) -> str:
        hashed_paths.append(path)
        return hash_file(path)

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(rwkv_scheduler, "_sha256_file", count_hash)

    first_key = rwkv_scheduler._rwkv_model_cache_key()
    assert rwkv_scheduler._rwkv_model_cache_key() == first_key
    assert hashed_paths == [model_path]

    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"model two")
    os.utime(
        replacement,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replacement.replace(model_path)
    assert model_path.stat().st_mtime_ns == original_stat.st_mtime_ns

    second_key = rwkv_scheduler._rwkv_model_cache_key()

    assert hashed_paths == [model_path, model_path]
    assert first_key is not None
    assert second_key is not None
    assert first_key["size"] == second_key["size"]
    assert first_key["sha256"] != second_key["sha256"]


def test_rwkv_model_cache_key_single_flights_concurrent_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "RWKV_trained_on_5000_10000.bin"
    model_path.write_bytes(b"shared model")
    hash_file = rwkv_scheduler._sha256_file
    hash_started = threading.Event()
    release_hash = threading.Event()
    second_lock_attempted = threading.Event()
    counter_lock = threading.Lock()
    hash_calls = 0

    class ObservedLock:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.counter_lock = threading.Lock()
            self.attempts = 0
            self.second_attempt_saw_locked: list[bool] = []

        def __enter__(self) -> None:
            with self.counter_lock:
                self.attempts += 1
                if self.attempts == 2:
                    self.second_attempt_saw_locked.append(self.lock.locked())
                    second_lock_attempted.set()
            self.lock.acquire()

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            self.lock.release()

    observed_lock = ObservedLock()

    def controlled_hash(path: Path) -> str:
        nonlocal hash_calls
        with counter_lock:
            hash_calls += 1
        hash_started.set()
        if not release_hash.wait(timeout=5):
            raise TimeoutError("timed out waiting to release model hash")
        return hash_file(path)

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(rwkv_scheduler, "_sha256_file", controlled_hash)
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_lock", observed_lock)
    results: list[dict[str, object] | None] = []
    failures: list[BaseException] = []

    def compute_key() -> None:
        try:
            results.append(rwkv_scheduler._rwkv_model_cache_key())
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(target=compute_key)
    second = threading.Thread(target=compute_key)
    first.start()
    try:
        assert hash_started.wait(timeout=5)
        second.start()
        assert second_lock_attempted.wait(timeout=5)
        assert observed_lock.second_attempt_saw_locked == [True]
    finally:
        release_hash.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert hash_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]


def test_rwkv_state_cache_restores_legacy_embedded_model_key_after_reinstall(
    monkeypatch,
    tmp_path,
) -> None:
    model_bytes = b"same bundled model"
    first_model_path = (
        tmp_path / "first-install" / "rwkv_inference" / "RWKV_trained_on_5000_10000.bin"
    )
    second_model_path = (
        tmp_path
        / "second-install"
        / "rwkv_inference"
        / "RWKV_trained_on_5000_10000.bin"
    )
    first_model_path.parent.mkdir(parents=True)
    second_model_path.parent.mkdir(parents=True)
    first_model_path.write_bytes(model_bytes)
    second_model_path.write_bytes(model_bytes)
    rows = [
        ((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500),
        ((41 * 86_400 + 3_700) * 1000, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: first_model_path,
    )

    profile_folder = tmp_path / "profile"
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=profile_folder, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]

    cache_dir = profile_folder / "rwkv-state-cache"
    metadata_path = cache_dir / "state-v1.meta.json"
    snapshot_path = cache_dir / "snapshot-v1.bin"
    legacy_model_key = {
        "path": str(first_model_path),
        "size": first_model_path.stat().st_size,
        "mtimeNs": first_model_path.stat().st_mtime_ns,
    }
    current_metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert current_metadata is not None
    legacy_metadata = {**current_metadata, "model": legacy_model_key}
    snapshot_metadata, snapshot, history = (
        rwkv_scheduler._decode_rwkv_state_cache_snapshot_file(
            snapshot_path.read_bytes()
        )
    )
    assert snapshot_metadata["model"] == current_metadata["model"]
    snapshot_path.write_bytes(
        rwkv_scheduler._encode_rwkv_state_cache_snapshot_file(
            metadata=legacy_metadata,
            snapshot=snapshot,
            history=history,
        )
    )
    metadata_path.write_text(
        json.dumps(legacy_metadata, separators=(",", ":"), sort_keys=True),
        encoding="utf8",
    )

    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: second_model_path,
    )
    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert restored_runtime.reviewed == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]

    compacted_metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert compacted_metadata is not None
    assert compacted_metadata["model"] == rwkv_scheduler._rwkv_model_cache_key()
    compacted_snapshot_metadata, _, _ = (
        rwkv_scheduler._decode_rwkv_state_cache_snapshot_file(
            snapshot_path.read_bytes()
        )
    )
    assert compacted_snapshot_metadata["model"] == compacted_metadata["model"]


def test_reviewer_rwkv_warmup_cache_replays_only_new_revlogs(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    snapshot_path = tmp_path / "rwkv-state-cache" / "snapshot-v1.bin"
    delta_path = tmp_path / "rwkv-state-cache" / "deltas-v1.log"
    snapshot_size = snapshot_path.stat().st_size
    delta_size = delta_path.stat().st_size

    rows.append((second_review, 1, 10, 100, 3, 2345, 2, 5, 2400))
    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert restored_runtime.reviewed == [(1, 3)]
    assert restored_runtime.answered_inputs[0].current_elapsed_seconds == 90_000
    assert snapshot_path.stat().st_size == snapshot_size
    assert delta_path.stat().st_size > delta_size

    delta_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(delta_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert delta_runtime.reviewed == [(1, 3)]
    assert delta_runtime.answered_inputs[0].current_elapsed_seconds == 90_000


@pytest.mark.parametrize("failed_checkpoint_day", [None, 8])
def test_reviewer_rwkv_warmup_recovers_from_checkpoint_after_past_sync(
    monkeypatch,
    tmp_path,
    failed_checkpoint_day: int | None,
) -> None:
    review_ids = {
        day: (day * 86_400 + 100) * 1000 for day in (0, 8, 12, 13, 14, 15, 16)
    }
    rows = [
        (review_ids[day], 1, 10, 100, ease, 1234, 1, day + 1, 2500)
        for day, ease in zip(
            (0, 8, 12, 14, 15, 16),
            (2, 3, 4, 2, 3, 4),
            strict=True,
        )
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    rows.append((review_ids[13], 1, 10, 100, 1, 1234, 1, 14, 2400))
    rows.sort()
    fully_decoded_paths: list[str] = []
    read_snapshot = rwkv_scheduler._read_rwkv_state_cache_snapshot_file

    def track_full_snapshot_decode(
        path: Path,
    ) -> tuple[
        dict[str, object],
        RwkvBackendCacheSnapshot,
        rwkv_scheduler.RwkvHistoricalReviewInputs,
    ]:
        fully_decoded_paths.append(path.name)
        if (
            failed_checkpoint_day is not None
            and path.name == f"checkpoint-v1-{review_ids[failed_checkpoint_day]}.bin"
        ):
            raise ValueError("simulated unreadable RWKV checkpoint")
        return read_snapshot(path)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_rwkv_state_cache_snapshot_file",
        track_full_snapshot_decode,
    )
    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    if failed_checkpoint_day is None:
        assert fully_decoded_paths == [f"checkpoint-v1-{review_ids[8]}.bin"]
        assert restored_runtime.reviewed == [
            (1, 4),
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
        ]
    else:
        assert fully_decoded_paths == [f"checkpoint-v1-{review_ids[8]}.bin"]
        assert restored_runtime.reviewed == [
            (1, 2),
            (1, 3),
            (1, 4),
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
        ]
    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    assert metadata["lastReviewId"] == review_ids[16]
    assert metadata["reviewCount"] == 7
    checkpoints = metadata["checkpoints"]
    assert isinstance(checkpoints, list)
    assert [
        {
            "lastReviewId": checkpoint["lastReviewId"],
            "reviewCount": checkpoint["reviewCount"],
        }
        for checkpoint in checkpoints
    ] == [
        {"lastReviewId": review_ids[8], "reviewCount": 2},
    ]
    assert all(
        rwkv_scheduler._rwkv_history_hash_is_valid(checkpoint["historyHash"])
        for checkpoint in checkpoints
    )


def test_reviewer_rwkv_successive_recoveries_reselect_exponential_checkpoints(
    monkeypatch,
    tmp_path,
) -> None:
    review_ids = {
        day: (day * 86_400 + 100) * 1000 for day in (0, 8, 11, 12, 13, 14, 15, 16, 32)
    }
    rows = [
        (review_ids[day], 1, 10, 100, ease, 1234, 1, day + 1, 2500)
        for day, ease in zip(
            (0, 8, 12, 14, 15, 16),
            (2, 3, 4, 2, 3, 4),
            strict=True,
        )
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    rows.append((review_ids[13], 1, 10, 100, 1, 1234, 1, 14, 2400))
    rows.sort()
    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    rows.extend(
        [
            (review_ids[11], 1, 10, 100, 2, 1234, 1, 12, 2450),
            (review_ids[32], 1, 10, 100, 3, 1234, 1, 33, 2350),
        ]
    )
    rows.sort()
    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert metadata is not None
    checkpoints = metadata["checkpoints"]
    assert isinstance(checkpoints, list)
    assert [
        (checkpoint["lastReviewId"], checkpoint["reviewCount"])
        for checkpoint in checkpoints
    ] == [
        (review_ids[16], 8),
    ]


def test_post_sync_refresh_replays_from_historical_checkpoint(
    monkeypatch,
    tmp_path,
) -> None:
    review_ids = {
        day: (day * 86_400 + 100) * 1000 for day in (0, 8, 12, 13, 14, 15, 16)
    }
    rows = [
        (review_ids[day], 1, 10, 100, ease, 1234, 1, day + 1, 2500)
        for day, ease in zip(
            (0, 8, 12, 14, 15, 16),
            (2, 3, 4, 2, 3, 4),
            strict=True,
        )
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    runtime.reviewed.clear()

    rows.append((review_ids[13], 1, 10, 100, 1, 1234, 1, 14, 2400))
    rows.sort()
    taskman, _progress_updates = _attach_progress_taskman(reviewer.mw)
    completed: list[bool] = []

    rwkv_scheduler.refresh_rwkv_state_after_sync(
        reviewer.mw,
        lambda: completed.append(True),
    )

    assert completed == [True]
    assert runtime.reviewed == [
        (1, 4),
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
    ]
    assert taskman.with_progress_kwargs is not None
    assert (
        taskman.with_progress_kwargs["label"]
        == _tr().qt_misc_review_history_after_sync()
    )
    assert taskman.with_progress_kwargs["uses_collection"] is True


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a sync that
# brings reviews older than the replay window says nothing and asks for
# nothing; once it has finished, RWKV reads the whole history again by itself.
def test_post_sync_refresh_ignores_reviews_older_than_eight_days(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    review_ids = {day: (day * 86_400 + 100) * 1000 for day in (0, 7, 8, 12, 14, 15, 16)}
    rows = [
        (review_ids[day], 1, 10, 100, ease, 1234, 1, day + 1, 2500)
        for day, ease in zip(
            (0, 8, 12, 14, 15, 16),
            (2, 3, 4, 2, 3, 4),
            strict=True,
        )
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        "aqt.utils.show_warning",
        lambda message, **_kwargs: warnings.append(message),
    )

    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    runtime.reviewed.clear()

    rows.append((review_ids[7], 1, 10, 100, 1, 1234, 1, 8, 2400))
    rows.sort()
    _taskman, _progress_updates = _attach_progress_taskman(reviewer.mw)
    completed: list[bool] = []

    rebuilds: list[dict[str, object]] = []
    build = rwkv_scheduler.build_rwkv_state_cache_with_progress

    def record_rebuild(mw: object, **kwargs: Any) -> None:
        # the sync itself only skips the old review
        metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
        assert metadata is not None
        assert metadata["ignoredReviewIds"] == [review_ids[7]]
        assert runtime.reviewed == []
        rebuilds.append(kwargs)
        build(mw, **kwargs)

    monkeypatch.setattr(
        rwkv_scheduler, "build_rwkv_state_cache_with_progress", record_rebuild
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)

    rwkv_scheduler.refresh_rwkv_state_after_sync(
        reviewer.mw,
        lambda: completed.append(True),
        remote_review_ids=(review_ids[7],),
    )

    assert completed == [True]
    # no message; the whole history is read again, the old review included
    assert warnings == []
    assert rebuilds == [{"force_rebuild": True}]
    assert len(runtime.reviewed) == len(rows)
    rebuilt_metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert rebuilt_metadata is not None
    assert "ignoredReviewIds" not in rebuilt_metadata
    assert (
        rwkv_scheduler._read_rwkv_state_cache_binary(
            reviewer,
            backend=backend,
        )
        is not None
    )


def test_stale_post_sync_failure_does_not_clobber_newer_ready_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.state = "pre-sync"
            self.restored: list[str] = []

        def cache_snapshot(self) -> str:
            return self.state

        def restore_cache_snapshot(self, snapshot: str) -> None:
            self.restored.append(snapshot)
            self.state = snapshot

    class DeferredTaskman:
        def __init__(self) -> None:
            self.future: Future[bool] | None = None
            self.on_done: Callable[[Future[bool]], None] | None = None

        def with_progress(
            self,
            task: Callable[[], bool],
            on_done: Callable[[Future[bool]], None],
            **_kwargs: object,
        ) -> None:
            self.future = Future()
            self.future.set_result(task())
            self.on_done = on_done

        def finish(self) -> None:
            assert self.future is not None
            assert self.on_done is not None
            self.on_done(self.future)

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[key] = None
    monkeypatch.setattr(
        rwkv_scheduler,
        "_warm_up_reviewer_backend",
        lambda reviewer, *, progress=None, additional_ignored_review_ids=(): False,
    )
    taskman = DeferredTaskman()
    reviewer.mw.taskman = taskman
    completed: list[bool] = []

    rwkv_scheduler.refresh_rwkv_state_after_sync(
        reviewer.mw,
        lambda: completed.append(True),
    )

    assert key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is True
    assert completed == []

    backend.state = "newer-ready"
    generation = rwkv_scheduler._reviewer_backend_warmup_generations[key]
    newer_identity = replace(_rwkv_resident_identity(), last_review_id=123)
    assert rwkv_scheduler._publish_reviewer_backend_state(
        key,
        newer_identity,
        expected_generation=generation,
    )

    taskman.finish()

    assert backend.restored == []
    assert backend.state == "newer-ready"
    assert rwkv_scheduler._reviewer_backend_warmup_states[key] == newer_identity
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is False
    assert completed == [True]


def test_post_sync_refresh_cleans_up_when_with_progress_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[key] = None
    restored: list[RwkvBackendCacheSnapshot] = []
    original_restore = backend.restore_cache_snapshot

    def restore(snapshot: RwkvBackendCacheSnapshot) -> None:
        restored.append(snapshot)
        original_restore(snapshot)

    monkeypatch.setattr(backend, "restore_cache_snapshot", restore)

    class Taskman:
        def with_progress(
            self,
            task: Callable[[], bool],
            on_done: Callable[[Future[bool]], None],
            **_kwargs: object,
        ) -> None:
            future: Future[bool] = Future()
            future.set_exception(RuntimeError("failed to launch progress task"))
            on_done(future)
            raise RuntimeError("failed after callback")

    reviewer.mw.taskman = Taskman()
    completed: list[bool] = []

    rwkv_scheduler.refresh_rwkv_state_after_sync(
        reviewer.mw,
        lambda: completed.append(True),
    )

    assert restored == []
    assert key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is False
    assert completed == [True]


def test_rwkv_state_cache_build_uses_modal_progress(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    prewarm_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda _reviewer, **kwargs: prewarm_calls.append(kwargs),
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    taskman, progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert taskman.with_progress_kwargs is not None
    assert taskman.with_progress_kwargs["immediate"] is True
    assert taskman.with_progress_kwargs["uses_collection"] is True
    assert taskman.with_progress_kwargs["title"] == _tr().qt_misc_review_history_title()
    assert prewarm_calls == [
        {
            "reason": "state cache build",
            "include_parent_scope": False,
        }
    ]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert any(
        update["value"] == 0
        and update["max"] == 2
        and _plain(update["label"]).startswith(
            "Collecting your reviews: 0 of 2 reviews"
        )
        for update in progress_updates
    )
    assert any(
        update["value"] == 2
        and update["max"] == 2
        and _plain(update["label"]).startswith(
            "Collecting your reviews: 2 of 2 reviews"
        )
        for update in progress_updates
    )
    assert any(
        update["value"] == 2
        and update["max"] == 2
        and _plain(update["label"])
        == "Reading your review history: 2 of 2 reviews, about 0s left"
        for update in progress_updates
    )


def test_rwkv_state_cache_build_skips_review_retrievability_cache_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert reviewer.mw.col.rwkv_retrievability_rows == []


def test_rwkv_state_cache_build_warns_when_only_session_state_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    warnings: list[str] = []
    tooltips: list[str] = []

    def warm_up(_mw: object, **kwargs: object) -> bool:
        on_error = kwargs.get("on_cache_persistence_error")
        assert callable(on_error)
        on_error(PermissionError("simulated locked cache file"))
        return True

    monkeypatch.setattr(rwkv_scheduler, "warm_up_rwkv_state", warm_up)
    monkeypatch.setattr(
        "aqt.utils.show_warning",
        lambda text, **_kwargs: warnings.append(text),
    )
    monkeypatch.setattr(
        "aqt.utils.tooltip",
        lambda text, **_kwargs: tooltips.append(text),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda *_args, **_kwargs: None,
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert tooltips == []
    assert len(warnings) == 1
    assert "ready for this session" in warnings[0]
    assert "simulated locked cache file" in warnings[0]
    assert str(tmp_path / "rwkv-state-cache") in warnings[0]
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is False


def test_warmup_keeps_resident_identity_when_cache_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    review_id = (42 * 86_400 + 1000) * 1000
    rows = [(review_id, 1, 10, 100, 3, 1234, 1, 5, 2500)]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    expected_history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)

    def fail_write(_path: Path, _data: bytes) -> None:
        raise OSError("simulated state-cache write failure")

    monkeypatch.setattr(rwkv_scheduler, "_atomic_write", fail_write)
    persistence_errors: list[Exception] = []

    assert (
        rwkv_scheduler._warm_up_reviewer_backend(
            reviewer,
            on_cache_persistence_error=persistence_errors.append,
        )
        is True
    )
    assert len(persistence_errors) == 1
    assert str(persistence_errors[0]) == "simulated state-cache write failure"

    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = rwkv_scheduler._reviewer_backend_warmup_states[warmup_key]
    assert resident_identity == rwkv_scheduler._resident_state_identity(
        expected_history
    )
    assert rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] == (
        rwkv_scheduler._reviewer_backend_warmup_generations.get(warmup_key, 0),
        resident_identity,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_read_rwkv_state_cache_metadata",
        lambda _reviewer: pytest.fail(
            "resident identity must not be recovered from stale disk metadata"
        ),
    )
    assert rwkv_scheduler._rwkv_ready_state_cache_history_identity(
        reviewer
    ) == rwkv_scheduler._resident_state_identity(expected_history)


def test_rwkv_state_cache_build_backfills_missing_review_retrievability_cache(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    reviewer.mw.col.rwkv_retrievability_rows.clear()
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(
        reviewer.mw,
        record_retrievability_cache=True,
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


def test_rwkv_state_cache_build_backfills_missing_review_cache_when_already_warm(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    reviewer.mw.col.rwkv_retrievability_rows.clear()
    runtime.reviewed.clear()
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(
        reviewer.mw,
        record_retrievability_cache=True,
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


def test_ensure_rwkv_calibration_data_generates_once_and_restores_state(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    before = backend.cache_snapshot()
    assert reviewer.mw.col.rwkv_retrievability_rows == []
    assert rwkv_scheduler.rwkv_calibration_data_available(reviewer.mw) is False

    runtime.reviewed.clear()
    assert rwkv_scheduler.ensure_rwkv_calibration_data(reviewer.mw) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert backend.cache_snapshot() == before
    assert rwkv_scheduler.rwkv_calibration_data_available(reviewer.mw) is True
    assert {row[4] for row in reviewer.mw.col.rwkv_retrievability_rows} == {
        rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
        rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD,
    }

    runtime.reviewed.clear()
    assert rwkv_scheduler.ensure_rwkv_calibration_data(reviewer.mw) is True
    assert runtime.reviewed == []


def test_rwkv_calibration_recompute_uses_fsrs_validation_folds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_active_fsrs_validation_fold_indices",
        lambda _reviewer, *, last_review_id: {first_review: 3},
    )

    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True
    cache_rows = {
        review_id: (sample_role, fold_index)
        for review_id, _prediction, _source, _updated_at, sample_role, fold_index in (
            reviewer.mw.col.rwkv_retrievability_rows
        )
    }
    assert cache_rows == {
        first_review: (
            rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD,
            3,
        ),
        second_review: (
            rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
            -1,
        ),
    }


def test_rwkv_calibration_fold_roles_fall_back_to_chronological_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _rwkv_checkpoint_test_history(10)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_active_fsrs_validation_fold_indices",
        lambda _reviewer, *, last_review_id: None,
    )

    sample_roles, fold_indices = rwkv_scheduler._rwkv_calibration_fold_role_maps(
        object(),
        history,
    )

    assert [sample_roles[review_id] for review_id in history.review_ids] == [
        *[rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT] * 7,
        *[rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD] * 3,
    ]
    assert [fold_indices[review_id] for review_id in history.review_ids] == [
        *[-1] * 7,
        *[0] * 3,
    ]


def test_rwkv_calibration_alignment_rejects_legacy_fold_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_active_fsrs_validation_fold_indices",
        lambda _reviewer, *, last_review_id: {1_000: 3},
    )

    class DB:
        def all(self, _sql: str, _last_review_id: int) -> list[tuple[int, int]]:
            return [(1_000, 0)]

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert (
        rwkv_scheduler._rwkv_calibration_test_folds_match_fsrs(
            reviewer,
            last_review_id=1_000,
        )
        is False
    )


def test_active_fsrs_validation_folds_follow_graph_role_precedence() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
create table search_stats_fsrs_review_retrievability (
  revlog_id integer not null,
  prediction real not null,
  source text not null,
  updated_at integer not null,
  sample_role text not null,
  fold_index integer not null
)
"""
    )
    connection.executemany(
        """
insert into search_stats_fsrs_review_retrievability
  (revlog_id, prediction, source, updated_at, sample_role, fold_index)
values (?, 0.5, 'test', ?, ?, ?)
""",
        [
            (1_000, 10, "final_fit", -1),
            (1_000, 10, "validation_fold", 2),
            (2_000, 10, "validation_fold", 3),
            (2_000, 11, "post_optimization", -1),
            (3_000, 12, "validation_fold", 4),
            (4_000, 13, "validation_fold", 1),
        ],
    )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[int, int]]:
            return cast(list[tuple[int, int]], connection.execute(sql, args).fetchall())

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert rwkv_scheduler._active_fsrs_validation_fold_indices(
        reviewer,
        last_review_id=3_000,
    ) == {1_000: 2, 3_000: 4}


def test_rwkv_state_cache_build_satisfies_sse_explicit_revlog_contract(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
            require_retrievability_cache=True,
        )
        is True
    )

    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review],
    ) == {
        "column": "search_stats_rwkv_review_retrievability",
        "data": [
            (first_review, pytest.approx(0.45)),
            (second_review, pytest.approx(0.45)),
        ],
    }
    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review + 1],
    ) == {"column": None, "data": []}


def test_srs_benchmark_state_cache_build_satisfies_sse_explicit_revlog_contract(
    monkeypatch,
    tmp_path,
) -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def item(self) -> float:
            return 0.72

    class Process:
        def __init__(self) -> None:
            self.query_rows: list[dict[str, object]] = []
            self.answer_rows: list[dict[str, object]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            self.query_rows.append(row)
            return Probability()

        def process_row(self, row: dict[str, object]) -> object:
            self.answer_rows.append(row)
            return object()

    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    process = Process()
    set_reviewer_backend(SrsBenchmarkRwkvReviewerBackend(process=process))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
            require_retrievability_cache=True,
        )
        is True
    )

    assert [row["rating"] for row in process.query_rows] == [1, 1]
    assert [row["rating"] for row in process.answer_rows] == [2, 3]
    assert process.query_rows[0]["elapsed_seconds"] > 0
    assert process.answer_rows[0]["elapsed_days"] == -1
    assert process.answer_rows[0]["elapsed_seconds"] == -1
    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review],
    ) == {
        "column": "search_stats_rwkv_review_retrievability",
        "data": [
            (first_review, pytest.approx(0.72)),
            (second_review, pytest.approx(0.72)),
        ],
    }


def test_rwkv_state_cache_force_rebuild_replays_full_history(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]

    runtime.reviewed.clear()
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    assert runtime.reviewed == []

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
        )
        is True
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert runtime.restored_cache_states[-1] == b"runtime-cache"
    assert reviewer.mw.col.rwkv_retrievability_rows == []


def test_stateful_backend_uses_runtime_bulk_warmup_for_empty_state() -> None:
    class BulkWarmUpRuntime:
        def __init__(self) -> None:
            self.reviews: list[RwkvReviewInput] = []

        def warm_up_reviews(
            self,
            reviews: list[RwkvReviewInput],
            *,
            review_ids: list[int] | None,
            prediction_recorder: Any,
            progress: Any,
        ) -> RwkvBackendCacheSnapshot:
            self.reviews = list(reviews)
            if prediction_recorder is not None and review_ids is not None:
                prediction_recorder(review_ids[0], 0.31)
                prediction_recorder(review_ids[1], 0.42)
            if progress is not None:
                progress(RwkvWarmUpProgress(processed_reviews=2, total_reviews=2))

            return RwkvBackendCacheSnapshot(
                card_states={1: b"card-1", 2: b"card-2"},
                note_states={10: b"note-10", 20: b"note-20"},
                deck_states={100: b"deck-100"},
                preset_states={1000: b"preset-1000"},
                global_state=b"global",
                runtime_state=b"runtime",
            )

        def review(self, **kwargs: object) -> RwkvReviewTransition:
            raise AssertionError("bulk warm-up should replace per-review replay")

    first = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        is_query=False,
        ease=2,
        duration_millis=1234,
        card_type=2,
        card_queue=2,
        card_due=50,
        interval_days=4,
        ease_factor=2500,
        reps=5,
        lapses=1,
        day_offset=42,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=7,
        current_elapsed_seconds=604800,
    )
    second = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=2, note_id=20, deck_id=100, preset_id=1000),
        is_query=False,
        ease=3,
        duration_millis=2345,
        card_type=2,
        card_queue=2,
        card_due=50,
        interval_days=5,
        ease_factor=2400,
        reps=6,
        lapses=1,
        day_offset=43,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=8,
        current_elapsed_seconds=691200,
    )
    runtime = BulkWarmUpRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    recorded: list[tuple[int, float]] = []
    progress_updates: list[RwkvWarmUpProgress] = []

    backend.warm_up(
        [first, second],
        review_ids=[101, 102],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
        progress=progress_updates.append,
    )

    snapshot = backend.cache_snapshot()
    assert runtime.reviews == [first, second]
    assert recorded == [(101, 0.31), (102, 0.42)]
    assert snapshot.card_states == {1: b"card-1", 2: b"card-2"}
    assert snapshot.note_states == {10: b"note-10", 20: b"note-20"}
    assert snapshot.deck_states == {100: b"deck-100"}
    assert snapshot.preset_states == {1000: b"preset-1000"}
    assert snapshot.global_state == b"global"
    assert snapshot.runtime_state is None
    assert progress_updates[-1] == RwkvWarmUpProgress(
        processed_reviews=2,
        total_reviews=2,
    )


def test_stateful_backend_keeps_resident_runtime_state_out_of_python() -> None:
    runtime = _ResidentCacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviews = [
        _warm_up_review_input(card_id=1, note_id=10, ease=2),
        _warm_up_review_input(card_id=2, note_id=20, ease=3),
    ]

    backend.warm_up(reviews)

    assert runtime.return_snapshot_flags == [False]
    assert backend._card_states == {}
    assert backend._note_states == {}
    assert backend._deck_states == {}
    assert backend._preset_states == {}
    assert backend._global_state is None
    snapshot = backend.cache_snapshot()
    assert snapshot.card_states == {1: b"card-1-2", 2: b"card-2-3"}
    assert snapshot.note_states == {10: b"note-10-2", 20: b"note-20-3"}
    assert snapshot.global_state == b"global-2"


def test_stateful_backend_records_final_warmup_checkpoint() -> None:
    runtime = _ResidentCacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviews = [
        _warm_up_review_input(card_id=1, note_id=10, ease=2),
        _warm_up_review_input(card_id=2, note_id=20, ease=3),
    ]
    recorded: list[int] = []

    backend.warm_up(
        reviews,
        snapshot_after_reviews=[1, len(reviews)],
        snapshot_recorder=lambda review_count, _snapshot: recorded.append(review_count),
    )

    assert recorded == [1, 2]


def test_stateful_backend_continues_resident_state_after_cache_restore() -> None:
    runtime = _ResidentCacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    restored = RwkvBackendCacheSnapshot(
        card_states={1: b"card-1-2"},
        note_states={10: b"note-10-2"},
        deck_states={100: b"deck-100-2"},
        preset_states={1000: b"preset-1000-2"},
        global_state=b"global-1",
        runtime_state=b"runtime",
    )

    backend.restore_cache_snapshot(restored)
    backend.warm_up([_warm_up_review_input(card_id=2, note_id=20, ease=3)])

    assert runtime.reset_count == 0
    snapshot = backend.cache_snapshot()
    assert snapshot.card_states == {1: b"card-1-2", 2: b"card-2-3"}
    assert snapshot.global_state == b"global-2"
    assert backend._card_states == {}


def test_stateful_backend_answer_and_undo_restore_resident_runtime_state() -> None:
    runtime = _ResidentCacheRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    backend.warm_up([_warm_up_review_input(card_id=1, note_id=10, ease=2)])
    before = backend.cache_snapshot()

    counter.set(1)
    backend.review_answered(
        reviewer=reviewer,
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        ease=3,
    )
    assert backend.cache_snapshot().card_states[1] == b"card-1-3"

    assert backend.answer_undone(1, 2) == 1
    assert backend.cache_snapshot() == before
    assert backend._card_states == {}


def test_warmup_capable_backend_records_review_retrievability_cache(tmp_path) -> None:
    class Backend:
        def warm_up(
            self,
            reviews: list[RwkvReviewInput],
            *,
            review_ids: list[int] | None = None,
            prediction_recorder: Any = None,
            progress: Any = None,
        ) -> None:
            assert [review.identity.card_id for review in reviews] == [1, 2]
            if prediction_recorder is not None and review_ids is not None:
                prediction_recorder(review_ids[0], 0.21)
                prediction_recorder(review_ids[1], 0.32)
            if progress is not None:
                progress(RwkvWarmUpProgress(processed_reviews=2, total_reviews=2))

    backend = Backend()
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    progress_labels: list[str] = []

    rwkv_scheduler._warm_up_rwkv_reviews(
        reviewer,
        backend,
        backend.warm_up,
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        review_ids=[101, 102],
        progress=lambda label, value, max_value: progress_labels.append(label),
        label="Building RWKV state cache",
    )

    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (101, pytest.approx(0.21), "rwkv_state_cache_build"),
        (102, pytest.approx(0.32), "rwkv_state_cache_build"),
    ]
    assert any(
        _plain(label).startswith("Building RWKV state cache: 2 of 2 reviews")
        for label in progress_labels
    )


def test_state_cache_build_skips_prediction_metadata_without_cache_rows(
    tmp_path,
) -> None:
    class Backend:
        def warm_up(
            self,
            reviews: list[RwkvReviewInput],
            **kwargs: object,
        ) -> None:
            assert len(reviews) == 2
            assert kwargs.get("prediction_recorder") is None

    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    backend = Backend()

    rwkv_scheduler._warm_up_rwkv_reviews(
        reviewer,
        backend,
        backend.warm_up,
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        review_ids=[101, 102],
        progress=None,
        label="Building RWKV state cache",
        record_retrievability_cache=False,
    )


def test_embedded_warmup_batches_prediction_recording() -> None:
    from aqt.rwkv_srs_benchmark import _record_warm_up_predictions

    class Recorder:
        def __call__(self, review_id: int, retrievability: float) -> None:
            pytest.fail("batch-capable recorder should not be called per prediction")

        def record_many(self, rows: list[tuple[int, float]]) -> None:
            self.rows.append(rows)

        def __init__(self) -> None:
            self.rows: list[list[tuple[int, float]]] = []

    recorder = Recorder()
    _record_warm_up_predictions(
        recorder,
        [101, 102, 103, 104],
        1,
        [(0, 0.21), (2, 0.43), (9, 0.99)],
    )

    assert recorder.rows == [[(102, 0.21), (104, 0.43)]]


def test_startup_loads_usable_rwkv_state_cache_without_a_window(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    prewarm_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda _reviewer, **kwargs: prewarm_calls.append(kwargs),
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))
    taskman, progress_updates = _attach_progress_taskman(reviewer.mw)
    history_builds = 0
    history_kwargs: list[dict[str, object]] = []
    build_history = rwkv_scheduler._historical_rwkv_review_inputs

    def counted_history_build(*args: object, **kwargs: object) -> object:
        nonlocal history_builds
        history_builds += 1
        history_kwargs.append(kwargs)
        return build_history(*args, **kwargs)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        counted_history_build,
    )

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(reviewer.mw)

    # pins spec/scheduling.md#sched.rwkv-startup-no-window: no window opens,
    # and the restore runs off the main thread all the same
    assert taskman.with_progress_kwargs is None
    assert taskman.background_runs == 1
    assert progress_updates == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    assert restored_runtime.reviewed == []
    assert prewarm_calls == [
        {
            "reason": "startup cache load",
            "include_parent_scope": False,
        }
    ]
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is False
    assert history_builds == 1
    # the whole-history read runs in short steps, so the collection and the
    # machine are free between them while the user works (spec
    # sched.rwkv-recordings-automatic)
    assert [kwargs.get("between_steps") for kwargs in history_kwargs] == [
        rwkv_scheduler._split_whole_history_query
    ]


def test_deck_browser_counts_wait_for_startup_cache_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[int, object | None]] = []
    completed: list[bool] = []
    refreshes: list[str] = []
    prewarm_calls: list[int] = []

    class Taskman:
        """Holds the background work until the test releases it, so the deck
        list can be asked for its counts while the load still runs."""

        progress_task: Callable[[], bool] | None = None
        progress_done: Callable[[Future[bool]], None] | None = None

        def run_on_main(self, callback: Callable[[], None]) -> None:
            callback()

        def with_progress(
            self,
            task: Callable[[], bool],
            on_done: Callable[[Future[bool]], None],
            **_kwargs: object,
        ) -> None:
            raise AssertionError("the start-up load must open no window")

        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool = True,
        ) -> None:
            if self.progress_task is None:
                # the start-up load: held until the test releases it
                self.progress_task = cast(Callable[[], bool], task)
                self.progress_done = cast(Callable[[Future[bool]], None], on_done)
                return
            # the deck list's own work, which runs while that load is held
            future: Future[object] = Future()
            future.set_result(task())
            on_done(future)

    taskman = Taskman()
    mw = SimpleNamespace(
        taskman=taskman,
        state="deckBrowser",
        onRefreshTimer=lambda: refreshes.append("refresh"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache",
        lambda _mw, *, progress=None: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_score_prewarm_work_for_deck",
        lambda _reviewer, *, deck_id, reason: prewarm_calls.append(deck_id),
    )

    rwkv_scheduler.load_rwkv_state_cache_with_progress(mw)

    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is True
    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10],
        should_continue=lambda: True,
        on_update=lambda deck_id, tree: updates.append((deck_id, tree)),
        on_done=completed.append,
    )
    assert updates == []
    assert completed == [False]
    assert prewarm_calls == []

    assert taskman.progress_task is not None
    assert taskman.progress_done is not None
    future: Future[bool] = Future()
    future.set_result(taskman.progress_task())
    taskman.progress_done(future)

    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False
    assert refreshes == ["refresh"]


def test_startup_load_skips_full_cache_preflight(monkeypatch) -> None:
    config_reads = 0
    load_calls: list[tuple[object, bool]] = []

    class Decks:
        def all_config(self) -> list[dict[str, object]]:
            nonlocal config_reads
            config_reads += 1
            return [
                {
                    "rwkvReviewEnabled": True,
                    "rwkvReviewDynamicPresetReplay": True,
                }
            ]

    monkeypatch.setattr(
        rwkv_scheduler,
        "rwkv_state_cache_usable",
        lambda *_args, **_kwargs: pytest.fail(
            "startup load must not run a separate full validation"
        ),
    )

    def load(
        mw: object,
        *,
        build_if_unavailable: bool = False,
    ) -> None:
        load_calls.append((mw, build_if_unavailable))

    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache_with_progress",
        load,
    )
    mw = SimpleNamespace(col=SimpleNamespace(decks=Decks()))

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(mw)

    assert config_reads == 1
    assert load_calls == [(mw, True)]


def test_startup_cache_load_can_wait_until_after_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    mw = SimpleNamespace()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_invalidate_reviewer_backend_runtime_state_for_profile_open",
        lambda: events.append("invalidate"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda _reviewer: rwkv_scheduler._RwkvCollectionConfigState(True, False),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache_with_progress",
        lambda _mw, *, build_if_unavailable=False: events.append(
            f"load:{build_if_unavailable}"
        ),
    )

    rwkv_scheduler.begin_rwkv_state_cache_startup(mw)
    assert events == ["invalidate"]
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is True

    events.append("sync")
    rwkv_scheduler.finish_rwkv_state_cache_startup(mw)

    assert events == ["invalidate", "sync", "load:True"]


def test_disabled_rwkv_clears_deferred_startup_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mw = SimpleNamespace()
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda _reviewer: rwkv_scheduler._RwkvCollectionConfigState(False, False),
    )

    rwkv_scheduler.begin_rwkv_state_cache_startup(mw)
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is True

    rwkv_scheduler.finish_rwkv_state_cache_startup(mw)
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False


def test_startup_load_launch_failure_builds_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[object] = []

    class Taskman:
        def run_on_main(self, callback: Callable[[], None]) -> None:
            callback()

        def with_progress(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated launch failure")

    mw = SimpleNamespace(taskman=Taskman())
    monkeypatch.setattr(
        rwkv_scheduler,
        "_start_rwkv_state_cache_build",
        builds.append,
    )

    rwkv_scheduler.load_rwkv_state_cache_with_progress(
        mw,
        build_if_unavailable=True,
    )

    assert builds == [mw]
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False


def test_startup_cache_miss_builds_before_refreshing_active_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    mw = SimpleNamespace(
        state="overview",
        onRefreshTimer=lambda: events.append("refresh"),
    )
    _attach_progress_taskman(mw)
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache",
        lambda _mw, *, progress=None: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_start_rwkv_state_cache_build",
        lambda _mw: events.append("build"),
    )

    rwkv_scheduler.load_rwkv_state_cache_with_progress(
        mw,
        build_if_unavailable=True,
    )

    assert events == ["build"]
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False


def test_queued_startup_build_skips_a_state_that_became_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    queued: list[Callable[[], None]] = []
    build_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "build_rwkv_state_cache_with_progress",
        lambda _mw, **kwargs: build_calls.append(kwargs),
    )

    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    reviewer.mw.taskman = SimpleNamespace(run_on_main=queued.append)

    rwkv_scheduler._start_rwkv_state_cache_build(reviewer.mw)

    assert len(queued) == 1
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    with rwkv_scheduler._reviewer_backend_state_lock:
        rwkv_scheduler._reviewer_backend_warmup_states[key] = _rwkv_resident_identity()

    queued[0]()

    assert build_calls == []


# Pins spec/scheduling.md#sched.rwkv-state-cache-startup-build
def test_startup_builds_the_state_and_the_calibration_data_without_asking(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "aqt.utils.ask_user_dialog",
        lambda *args, **kwargs: pytest.fail("the startup build must not ask"),
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    taskman, _progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(reviewer.mw)

    # the restore finds nothing and the build follows, neither in a window
    # (spec sched.rwkv-startup-no-window)
    assert taskman.with_progress_kwargs is None
    assert taskman.background_runs == 2
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


def test_reviewer_rwkv_undo_restores_previous_review_state() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    card_c = _rwkv_card(card_id=3, note_id=30, duration_millis=6789)

    counter.set(1)
    record_reviewer_answer(reviewer, card_a, ease=3)
    counter.set(2)
    record_reviewer_answer(reviewer, card_b, ease=4)
    assert update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.65)
    assert runtime.runtime_review_count == 2

    assert record_collection_undo(_undo_result(counter=2, next_counter=3)) == [2]
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.55)
    assert runtime.runtime_review_count == 1

    assert record_collection_undo(_undo_result(counter=1, next_counter=4)) == [1]
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.45)
    assert runtime.runtime_review_count == 0


def test_reviewer_rwkv_undo_restores_resident_runtime_state() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_restores: list[
                tuple[RwkvReviewIdentity, RwkvReviewerStateSnapshot]
            ] = []

        def restore_warm_up_state(
            self,
            identity: RwkvReviewIdentity,
            snapshot: RwkvReviewerStateSnapshot,
        ) -> None:
            self.resident_restores.append((identity, snapshot))

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    counter = _UndoCounter(reviewer)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    counter.set(1)
    record_reviewer_answer(reviewer, card, ease=3)
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is None
    resident_identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key],
        resident_identity,
    )
    record_collection_undo(_undo_result(counter=1, next_counter=2))

    assert len(runtime.resident_restores) == 1
    assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is None
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 2
    identity, snapshot = runtime.resident_restores[0]
    assert identity == RwkvReviewIdentity(1, 10, 100, 1000)
    assert snapshot.card_state is None
    assert snapshot.note_state is None
    assert snapshot.deck_state is None
    assert snapshot.preset_state is None
    assert snapshot.global_state is None


@pytest.mark.parametrize(
    ("operation", "handler_name"),
    [
        (record_collection_undo, "answer_undone"),
        (record_collection_redo, "answer_redone"),
    ],
)
def test_collection_undo_redo_skips_runtime_while_backend_state_is_pending(
    operation: Callable[[object], list[int]],
    handler_name: str,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def answer_undone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_undone")
            return 1

        def answer_redone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_redone")
            return 1

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[key] = identity
    rwkv_scheduler._reviewer_backend_warmup_generations[key] = 7
    rwkv_scheduler._reviewer_backend_warmup_pending_generations[key] = 7
    rwkv_scheduler._rwkv_memorised_history_identity_cache[key] = (7, identity)

    assert operation(_undo_result(counter=1, next_counter=2)) == []

    assert handler_name not in backend.calls
    assert key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[key] == 8
    assert rwkv_scheduler._reviewer_backend_warmup_pending_generations[key] == 7


@pytest.mark.parametrize(
    ("operation", "handler_name"),
    [
        (record_collection_undo, "answer_undone"),
        (record_collection_redo, "answer_redone"),
    ],
)
def test_collection_undo_redo_waits_for_backend_execution_without_losing_state(
    operation: Callable[[object], list[int]],
    handler_name: str,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def answer_undone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_undone")
            return 1

        def answer_redone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_redone")
            return 1

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[key] = identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[key] = (0, identity)

    held = threading.Event()
    release = threading.Event()

    def hold_execution_lock() -> None:
        with rwkv_scheduler._reviewer_backend_execution_lock:
            held.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_execution_lock)
    holder.start()
    assert held.wait(timeout=5)

    results: list[list[int]] = []
    completed = threading.Event()

    def invoke_operation() -> None:
        results.append(operation(_undo_result(counter=1, next_counter=2)))
        completed.set()

    worker = threading.Thread(target=invoke_operation)
    worker.start()
    try:
        assert not completed.wait(timeout=0.1)
        assert key in rwkv_scheduler._reviewer_backend_warmup_states
        assert rwkv_scheduler._rwkv_memorised_history_identity_cache[key] == (
            0,
            identity,
        )
    finally:
        release.set()
        holder.join(timeout=5)
        worker.join(timeout=5)

    assert completed.is_set()
    assert results == [[1]]
    assert backend.calls == [handler_name]
    assert rwkv_scheduler._reviewer_backend_warmup_states[key] is None
    assert key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[key] == 1


@pytest.mark.parametrize(
    ("operation", "handler_name"),
    [
        (record_collection_undo, "answer_undone"),
        (record_collection_redo, "answer_redone"),
    ],
)
def test_collection_undo_redo_handler_failure_invalidates_state_without_raising(
    operation: Callable[[object], list[int]],
    handler_name: str,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def answer_undone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_undone")
            raise RuntimeError("undo failed")

        def answer_redone(self, counter: int, next_counter: int | None) -> int:
            self.calls.append("answer_redone")
            raise RuntimeError("redo failed")

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    identity = _rwkv_resident_identity()
    rwkv_scheduler._reviewer_backend_warmup_states[key] = identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[key] = (0, identity)

    assert operation(_undo_result(counter=1, next_counter=2)) == []

    assert backend.calls == [handler_name]
    assert key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[key] == 1


def test_reviewer_rwkv_undo_queues_restored_cards_in_reverse_answer_order() -> None:
    reviewer = SimpleNamespace(_answeredIds=[1, 2, 3])

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [3, 2])

    assert reviewer._answeredIds == [1]
    assert rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 3
    assert rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 2
    assert not rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) is None


def test_reviewer_rwkv_undo_marks_queue_scores_stale_without_dropping_patch_base() -> (
    None
):
    runtime = _SharedReviewRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    rpc = _RwkvQueueScoreRpc()
    rpc.active_scores.update({1: 0.77, 2: 0.66})
    reviewer = _rwkv_reviewer(
        rpc=rpc, rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
    )
    reviewer._answeredIds = [1]
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.45,
        review_enabled=True,
        interval_override_used=False,
    )

    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.77, 2: 0.66}
    rwkv_scheduler._rwkv_review_queue_target_maps[100] = {1: 0.90, 2: 0.90}
    rwkv_scheduler._rwkv_review_queue_score_generations[100] = 12
    rwkv_scheduler._rwkv_review_queue_score_config_keys[100] = (
        (),
        (),
    )
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = (
        rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={
                512: [(1, _rwkv_review_input(card_id=1, note_id=10))]
            },
            loaded_rows=1,
            parsed_cards=1,
            cards_with_state=1,
            disabled_config_cards=0,
            eligible_cards=1,
            deck_configs=1,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )
    )

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [1])

    assert reviewer._answeredIds == []
    assert rpc.calls == []
    assert rpc.active_scores == {2: pytest.approx(0.66)}
    assert rpc.card_info_calls[-1] == {"card_id": 1, "retrievability": None}
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {
        100: {1: 0.77, 2: 0.66},
    }
    assert rwkv_scheduler._rwkv_review_queue_target_maps == {
        100: {1: 0.90, 2: 0.90},
    }
    assert rwkv_scheduler._rwkv_review_queue_score_generations == {}
    assert rwkv_scheduler._rwkv_review_queue_score_config_keys == {
        100: ((), ()),
    }
    cached = rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key]
    assert cached.dirty_card_ids == (1,)
    assert cached.session_answered_ids == ()
    rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    )
    assert dict(rows)["RWKV computed R"] == "45%"
    assert runtime.queries == [(1, None, None)]
    assert rpc.active_score_calls == []


def test_reviewer_rwkv_undo_refreshes_only_dirty_cached_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer._answeredIds = [1, 2, 3]
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    cached = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={
            512: [
                (card_id, _rwkv_review_input(card_id=card_id, note_id=card_id * 10))
                for card_id in (1, 2, 3)
            ]
        },
        loaded_rows=3,
        parsed_cards=3,
        cards_with_state=3,
        disabled_config_cards=0,
        eligible_cards=3,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
        session_answered_ids=(1, 2, 3),
    )
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = cached
    refreshed_card_ids: list[list[int]] = []

    def refresh(**kwargs: object) -> rwkv_scheduler.RwkvReviewInputBatchBuild:
        card_ids = list(cast(Sequence[int], kwargs["card_ids"]))
        refreshed_card_ids.append(card_ids)
        return replace(
            cached,
            inputs_by_batch_size={
                512: [
                    (
                        3,
                        replace(
                            _rwkv_review_input(card_id=3, note_id=30),
                            current_elapsed_days=7,
                        ),
                    )
                ]
            },
        )

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_from_backend_for_ids",
        refresh,
    )

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [3])
    result = rwkv_scheduler._cached_rwkv_review_input_batch_build(
        reviewer,
        cache_key,
    )

    assert reviewer._answeredIds == [1, 2]
    assert refreshed_card_ids == [[3]]
    assert result is not None
    assert result.session_answered_ids == (1, 2)
    assert result.dirty_card_ids == ()
    assert {
        card_id
        for inputs in result.inputs_by_batch_size.values()
        for card_id, _ in inputs
    } == {1, 2, 3}


def test_reviewer_rwkv_record_undo_does_not_clear_scores_without_reviewer() -> None:
    class Backend:
        def __init__(self) -> None:
            self.undone: list[tuple[int, int | None]] = []

        def answer_undone(self, counter: int, next_counter: int | None) -> bool:
            self.undone.append((counter, next_counter))
            return True

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer()
    reviewer.mw.col._backend = rpc
    previous_backend = set_reviewer_backend(Backend())
    try:
        record_collection_undo(_undo_result(counter=2, next_counter=3))
    finally:
        backend = set_reviewer_backend(previous_backend)

    assert isinstance(backend, Backend)
    assert backend.undone == [(2, 3)]
    assert rpc.calls == []


def test_reviewer_rwkv_redo_reapplies_review_state_with_new_counter() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    counter = _UndoCounter(reviewer)
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity()
    )

    counter.set(1)
    record_reviewer_answer(reviewer, card_a, ease=3)
    record_collection_undo(_undo_result(counter=1, next_counter=2))
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key],
        _rwkv_resident_identity(),
    )
    record_collection_redo(_undo_result(counter=2, next_counter=3))

    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.55)
    assert runtime.runtime_review_count == 1

    record_collection_undo(_undo_result(counter=3, next_counter=4))
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.45)
    assert runtime.runtime_review_count == 0


# Pins spec/ui.md#ui.fsrs7-no-rwkv-values
def test_fsrs7_card_gets_no_rwkv_prediction_and_no_card_info_rows() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(rwkv_review_enabled=False)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is False
    assert updated.good.normal.review.scheduled_days == 3
    assert updated.good.normal.review.fuzz_delta_days == 3
    # a loaded model predicts nothing for an FSRS-7 card
    assert current_reviewer_retrievability(reviewer, card) is None
    assert current_reviewer_diagnostics(reviewer, card, fallback_source="FSRS") is None
    assert (
        rwkv_card_info_rows(reviewer=reviewer, card=card, fallback_source="FSRS") == []
    )


# Pins spec/ui.md#ui.fsrs7-no-rwkv-values
def test_fsrs7_collection_prepares_no_rwkv_stats_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[bool] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "configure_reviewer_backend_from_environment",
        lambda: configured.append(True) or False,
    )
    set_reviewer_backend(None)
    published: list[tuple[str, list[tuple[int, float]]]] = []
    backend = SimpleNamespace(
        set_rwkv_stats_graph_scores=lambda **kwargs: published.append(
            (kwargs["search"], list(kwargs["scores"]))
        )
    )
    algorithm = {"schedulingAlgorithm": "fsrs7"}
    col = SimpleNamespace(
        get_config=lambda key, default=None: algorithm.get(key, default),
        _backend=backend,
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

    status = prepare_stats_retrievability_scores(reviewer, "deck:current")

    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    # no model is loaded, and stale RWKV scores are dropped
    assert configured == []
    assert published == [("deck:current", [])]

    algorithm["schedulingAlgorithm"] = "rwkvCurve"
    assert rwkv_scheduler.rwkv_collection_active(reviewer)


# Pins spec/scheduling.md#sched.rwkv-no-model-error
def test_startup_without_a_model_warns_instead_of_offering_a_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda reviewer: rwkv_scheduler._RwkvCollectionConfigState(True, False),
    )
    monkeypatch.setattr(rwkv_scheduler, "rwkv_model_available", lambda: False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_set_rwkv_state_cache_loading",
        lambda mw, loading: events.append(f"loading={loading}"),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_show_rwkv_model_missing", lambda mw: events.append("warn")
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache_with_progress",
        lambda *args, **kwargs: pytest.fail("no state to load without a model"),
    )

    rwkv_scheduler.finish_rwkv_state_cache_startup(SimpleNamespace())

    assert events == ["loading=False", "warn"]


# Pins spec/scheduling.md#sched.rwkv-no-model-error
def test_rwkv_instant_card_info_says_the_model_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_reviewer_backend(None)
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: False
    )
    reviewer = _rwkv_reviewer(
        rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    rows = rwkv_card_info_rows(reviewer=reviewer, card=card, fallback_source="FSRS")

    from aqt.utils import tr

    assert rows == [("RWKV computed R", tr.qt_misc_rwkv_model_not_found())]


# Pins spec/ui.md#ui.stats-one-algorithm
def test_rwkv_curve_collection_active_reads_the_algorithm() -> None:
    def reviewer(algorithm: str | None) -> SimpleNamespace:
        values = {"schedulingAlgorithm": algorithm}
        col = SimpleNamespace(get_config=lambda key, default=None: values.get(key))
        return SimpleNamespace(mw=SimpleNamespace(col=col))

    assert rwkv_scheduler.rwkv_curve_collection_active(reviewer("rwkvCurve"))
    for other in ("rwkvInstant", "fsrs7", None):
        assert not rwkv_scheduler.rwkv_curve_collection_active(reviewer(other))
    assert not rwkv_scheduler.rwkv_curve_collection_active(SimpleNamespace())


def test_rwkv_review_enabled_reads_legacy_fsrs_other_key() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1,
                "other": {
                    "jschoreels.fsrs": {
                        "rwkv_review_enabled": True,
                    },
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_review_enabled(reviewer, card) is True


def test_rwkv_review_enabled_reads_top_level_rwkv_key() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1,
                "other": {},
                "jschoreels.rwkv": {
                    "rwkv_review_enabled": True,
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_review_enabled(reviewer, card) is True


def test_rwkv_grade_order_enforcement_defaults_on_and_reads_rwkv_key() -> None:
    assert rwkv_scheduler._rwkv_review_enforce_grade_order_config({}) is True
    assert (
        rwkv_scheduler._rwkv_review_enforce_grade_order_config(
            {
                "jschoreels.rwkv": {
                    "rwkv_review_enforce_grade_order": False,
                }
            }
        )
        is False
    )


def test_reviewer_rwkv_curve_only_uses_curve_prediction_for_grade_intervals() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def predict_review_retrievability(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Instant-only prediction should not be used")

        def predict_review_uncached(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.calls.append("curve")
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=4,
                    good=9,
                    easy=18,
                ),
            )

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is True
    assert updated.good.normal.review.scheduled_days == 9
    assert backend.calls == ["curve"]
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


@pytest.mark.parametrize(
    ("curve", "instant", "instant_active"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, True),
        (True, True, False),
    ],
)
def test_one_algorithm_per_preset_both_rwkv_modes_read_as_curve(
    curve: bool, instant: bool, instant_active: bool
) -> None:
    """Pins spec/deck-options.md#deck-options.scheduler-choice (one algorithm)
    and spec/scheduling.md#sched.rwkv-instant-no-intervals"""
    reviewer = _rwkv_reviewer(
        rwkv_review_enabled=curve, rwkv_review_instant_order_enabled=instant
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    config = reviewer.mw.col.decks.config_dict_for_deck_id(100)

    assert rwkv_scheduler._rwkv_review_instant_order_enabled(config) is instant_active
    assert rwkv_scheduler.answer_intervals_hidden(reviewer, card) is instant_active


def test_reviewer_rwkv_instant_only_keeps_fsrs_intervals() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def predict_review_retrievability(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.calls.append("instant")
            return RwkvReviewPrediction(retrievability=0.62)

        def predict_review_uncached(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Curve prediction should not be used")

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        rwkv_review_enabled=False,
        rwkv_review_instant_order_enabled=True,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is False
    assert rwkv_scheduler.rwkv_review_active(reviewer, card) is True
    assert updated.good.normal.review.scheduled_days == 3
    assert updated.good.normal.review.fuzz_delta_days == 3
    assert backend.calls == ["instant"]
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


def test_prepare_reviewer_queue_order_works_with_rwkv_curve_disabled() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability={1: 0.80, 2: 0.20}[card.id])

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_curve_enabled=False,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 2]
    assert rpc.preset_id_calls == [[1, 2]]
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.80)), (2, pytest.approx(0.20))]


def test_prepare_reviewer_queue_order_uses_backend_deck_review_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.68 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": False,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewBatchSize": 64,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False}
    ]
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.68)),
    ]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]


@pytest.mark.parametrize(
    "new_gather_priority",
    [
        rwkv_scheduler._NEW_GATHER_PRIORITY_ASCENDING_RETRIEVABILITY,
        rwkv_scheduler._NEW_GATHER_PRIORITY_DESCENDING_RETRIEVABILITY,
    ],
)
def test_prepare_reviewer_queue_order_scores_new_gather_retrievability(
    monkeypatch: pytest.MonkeyPatch,
    new_gather_priority: int,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.74, 0.51]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=0,
                        card_queue=0,
                        card_due=50,
                        interval_days=0,
                        ease_factor=0,
                        reps=0,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="new",
                        target_retention=0.86,
                        batch_size=512,
                    ),
                    SimpleNamespace(
                        card_id=2,
                        note_id=20,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=51,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    ),
                ],
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=0,
        new_gather_priority=new_gather_priority,
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_review", lambda reviewer: True
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rwkv_scheduler.reviewer_queue_order_enabled(reviewer)
    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": True}
    ]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in cast(list[object], rpc.calls[0]["scores"])
    ] == [(1, pytest.approx(0.74)), (2, pytest.approx(0.51))]
    assert runtime.query_inputs[0].current_normal_state_kind == "new"


def test_prepare_reviewer_queue_order_reuses_backend_deck_review_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        current_elapsed_seconds=39 * 86_400,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": False,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewBatchSize": 64,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
        backend.review_input_answered(
            replace(runtime.query_inputs[0], is_query=False, ease=3)
        )
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False},
    ]
    assert len(rpc.calls) == 3
    assert [
        getattr(cast(list[object], call["scores"])[0], "retrievability")
        for call in rpc.calls
    ] == [pytest.approx(0.45), pytest.approx(0.45), pytest.approx(0.55)]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1,
        1,
    ]


def test_prepare_reviewer_queue_order_refreshes_answered_cached_backend_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id="addon-preset",
            card_type=2,
            card_queue=2,
            card_due=50,
            interval_days=4,
            ease_factor=2500,
            reps=5,
            lapses=1,
            day_offset=42,
            current_state_kind="normal",
            current_normal_state_kind="review",
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []
            self.card_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[row(39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(
                {
                    "card_ids": list(card_ids),
                    "include_suspended_review": include_suspended_review,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[row(0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_min_intervening_reviews=2,
    )
    reviewer._answeredIds = []
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
        reviewer._answeredIds.append(1)
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False},
    ]
    assert rpc.card_row_calls == [
        {
            "card_ids": [1],
            "include_suspended_review": False,
            "include_disabled_decks": False,
            "include_new_cards": False,
        }
    ]
    assert [
        review_input.current_elapsed_days for review_input in runtime.query_inputs
    ] == [
        39,
        0,
    ]
    scores = cast(list[object], rpc.calls[-1]["scores"])
    assert scores[0].intervening_reviews == 0


def test_cached_queue_inputs_add_newly_eligible_answered_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(card_id: int, elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=card_id,
            note_id=card_id * 10,
            deck_id=100,
            preset_id="addon-preset",
            card_type=2,
            card_queue=2,
            card_due=50,
            interval_days=4,
            ease_factor=2500,
            reps=5,
            lapses=1,
            day_offset=42,
            current_state_kind="normal",
            current_normal_state_kind="review",
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.card_row_calls: list[list[int]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[row(1, 39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(list(card_ids))
            return SimpleNamespace(
                rows=[row(2, 0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    backend = RwkvStatefulReviewerBackend(_SharedReviewRuntime())
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_min_intervening_reviews=2,
    )
    reviewer._answeredIds = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_warm_up_reviewer_backend",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        reviewer._answeredIds.append(2)
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.card_row_calls == [[2]]
    assert set(rpc.active_scores) == {1, 2}


def test_cached_queue_input_refresh_retries_after_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer._answeredIds = [2]
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    cached = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, _rwkv_review_input(card_id=1, note_id=10))]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = cached
    refreshed = replace(
        cached,
        inputs_by_batch_size={512: [(2, _rwkv_review_input(card_id=2, note_id=20))]},
    )
    responses = iter([None, refreshed])
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_from_backend_for_ids",
        lambda **kwargs: next(responses),
    )

    first = rwkv_scheduler._cached_rwkv_review_input_batch_build(reviewer, cache_key)
    second = rwkv_scheduler._cached_rwkv_review_input_batch_build(reviewer, cache_key)

    assert first is not None
    assert first.session_answered_ids == ()
    assert second is not None
    assert second.session_answered_ids == (2,)
    assert {
        card_id
        for inputs in second.inputs_by_batch_size.values()
        for card_id, _ in inputs
    } == {1, 2}


def test_cached_queue_input_refresh_excludes_cards_from_other_decks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer._answeredIds = [2]
    reviewer.mw.col.db = SimpleNamespace(
        list=lambda sql: [] if "from cards" in sql else pytest.fail(sql)
    )
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    cached = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, _rwkv_review_input(card_id=1, note_id=10))]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = cached
    outside_input = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        identity=RwkvReviewIdentity(
            card_id=2,
            note_id=20,
            deck_id=200,
            preset_id=2000,
        ),
    )
    refreshed = replace(
        cached,
        inputs_by_batch_size={512: [(2, outside_input)]},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_from_backend_for_ids",
        lambda **kwargs: refreshed,
    )

    result = rwkv_scheduler._cached_rwkv_review_input_batch_build(
        reviewer,
        cache_key,
    )

    assert result is not None
    assert {
        card_id
        for inputs in result.inputs_by_batch_size.values()
        for card_id, _ in inputs
    } == {1}
    assert result.session_answered_ids == (2,)


def test_current_deck_count_scoring_uses_active_reviewer_answered_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id="addon-preset",
            card_type=2,
            card_queue=2,
            card_due=50,
            interval_days=4,
            ease_factor=2500,
            reps=5,
            lapses=1,
            day_offset=42,
            current_state_kind="normal",
            current_normal_state_kind="review",
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.card_row_calls: list[list[int]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[row(39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(list(card_ids))
            return SimpleNamespace(
                rows=[row(0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer._answeredIds = [1]
    reviewer.mw.reviewer = reviewer
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        rwkv_scheduler.prepare_current_deck_review_queue_scores(
            reviewer.mw,
            reason="overview counts",
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.card_row_calls == [[1]]
    scores = cast(list[object], rpc.calls[-1]["scores"])
    assert scores[0].intervening_reviews == 0
    assert runtime.query_inputs[-1].current_elapsed_days == 0


def test_prepare_reviewer_queue_order_candidate_refresh_scores_stale_window() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability=card.id / 1000)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        batch_size=64,
        card_count=65,
        rwkv_candidate_refresh_enabled=True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        stale_scores = [(1, 0.99)] + [
            (card_id, card_id / 100) for card_id in range(2, 66)
        ]
        rwkv_scheduler._set_rwkv_review_queue_scores(
            reviewer,
            100,
            stale_scores,
            fresh_for_backend_state=False,
        )
        rpc.calls.clear()

        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == list(range(2, 66))
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    score_pairs = [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ]
    assert score_pairs[0] == (1, pytest.approx(0.99))
    assert score_pairs[1] == (2, pytest.approx(0.002))
    assert score_pairs[-1] == (65, pytest.approx(0.065))


def test_rwkv_candidate_refresh_relative_overdueness_uses_card_targets() -> None:
    assert rwkv_scheduler._rwkv_review_candidate_refresh_card_ids(
        {"reviewOrder": 12},
        {1: 0.90, 2: 0.79},
        {1: 0.95, 2: 0.80},
        limit=1,
    ) == [1]


def test_prepare_reviewer_queue_order_skips_when_instant_order_disabled() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Instant scoring should be disabled")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_instant_order_enabled=False,
    )
    previous_backend = set_reviewer_backend(Backend())
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    assert rpc.calls[0]["scores"] == []


def test_refresh_answered_card_queue_score_replaces_stale_score() -> None:
    class Backend:
        def __init__(self) -> None:
            self.retrievability_by_card_id = {1: 0.20, 2: 0.40}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=self.retrievability_by_card_id[card.id]
            )

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        backend.retrievability_by_card_id[1] = 0.95
        rwkv_scheduler.refresh_answered_card_queue_score(reviewer, reviewer.cards[1])
    finally:
        set_reviewer_backend(previous_backend)

    assert len(rpc.calls) == 1
    assert len(rpc.patch_calls) == 1
    patch = rpc.patch_calls[0]
    assert patch.deck_id == 100
    assert patch.card_id == 1
    assert patch.score.retrievability == pytest.approx(0.95)
    assert rpc.active_scores == {
        1: pytest.approx(0.95),
        2: pytest.approx(0.40),
    }


def test_answered_card_queue_score_patch_can_remove_score() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )

    assert rwkv_scheduler._patch_answered_card_rwkv_review_queue_score(
        reviewer,
        100,
        1,
        None,
        target_retention=None,
    )

    assert not rpc.patch_calls[0].HasField("score")
    assert rpc.active_scores == {2: pytest.approx(0.75)}
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(
        reviewer,
        100,
    ) == {2: pytest.approx(0.75)}


def test_invalidate_reviewer_queue_for_card_answer_preserves_queue_scores() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )
    rpc.calls.clear()

    rwkv_scheduler.invalidate_reviewer_queue_for_card_answer(
        reviewer,
        reviewer.cards[1],
    )

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.25)), (2, pytest.approx(0.75))]


def test_invalidate_reviewer_queue_for_child_card_preserves_current_deck_scores() -> (
    None
):
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer.cards[1].did = 101

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )
    rpc.calls.clear()

    rwkv_scheduler.invalidate_reviewer_queue_for_card_answer(
        reviewer,
        reviewer.cards[1],
    )

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.25)), (2, pytest.approx(0.75))]


def test_update_reviewer_queue_intervening_reviews_patches_session_cards_only() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        card_count=4,
        rwkv_min_intervening_reviews=1,
    )
    reviewer._answeredIds = [1, 3, 1, 2]

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (4, 0.50)],
    )
    rpc.calls.clear()

    rwkv_scheduler.update_reviewer_queue_intervening_reviews(
        reviewer,
        reviewer.cards[2],
    )

    assert rpc.calls == []
    assert len(rpc.intervening_calls) == 1
    request = rpc.intervening_calls[0]
    assert request.deck_id == 100
    assert [(item.card_id, item.intervening_reviews) for item in request.items] == [
        (1, 1),
        (2, 0),
    ]


def test_prepare_reviewer_queue_order_reuses_resolved_preset_ids() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability=0.80)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 2, 1, 2]
    assert rpc.preset_id_calls == [[1, 2]]


def test_set_rwkv_review_queue_scores_prefers_raw_backend() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

        def set_rwkv_review_queue_scores(
            self,
            *,
            deck_id: int,
            scores: list[object],
        ) -> None:
            raise AssertionError("raw queue score setter should be used")

    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )

    assert len(rpc.raw_calls) == 1
    request = rpc.raw_calls[0]
    assert request.deck_id == 100
    assert [(score.card_id, score.retrievability) for score in request.scores] == [
        (1, pytest.approx(0.25)),
        (2, pytest.approx(0.75)),
    ]


def test_set_rwkv_review_queue_scores_includes_session_intervening_reviews() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    rpc = Rpc()
    reviewer = SimpleNamespace(
        _answeredIds=[1, 3, 1, 2],
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                decks=SimpleNamespace(
                    config_dict_for_deck_id=lambda deck_id: {
                        "rwkvReviewMinInterveningReviews": 2,
                    }
                ),
            )
        ),
    )

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (3, 0.60), (4, 0.50)],
    )

    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[1].intervening_reviews == 1
    assert scores_by_card_id[2].intervening_reviews == 0
    assert not scores_by_card_id[3].HasField("intervening_reviews")
    assert not scores_by_card_id[4].HasField("intervening_reviews")


def test_set_rwkv_review_queue_scores_includes_revlog_intervening_reviews_for_deck_tree() -> (
    None
):
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    class DB:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def all(self, sql: str, *args: object) -> list[tuple[int]]:
            self.calls.append((sql, args))
            assert "from revlog r" in sql
            assert "join cards c on c.id = r.cid" in sql
            assert (
                "(case when c.odid != 0 then c.odid else c.did end) in (100,101)"
            ) in sql
            assert args == (3,)
            return [(2,), (1,), (2,)]

    class Decks:
        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"rwkvReviewMinInterveningReviews": 3}

    rpc = Rpc()
    db = DB()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=db,
                decks=Decks(),
            )
        )
    )

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (3, 0.50)],
    )

    assert len(db.calls) == 1
    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[2].intervening_reviews == 0
    assert scores_by_card_id[1].intervening_reviews == 1
    assert not scores_by_card_id[3].HasField("intervening_reviews")


def test_set_rwkv_review_queue_scores_includes_target_retention() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
        target_retentions_by_card_id={1: 0.50, 2: 1.25, 3: 0.40},
    )

    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[1].target_retention == pytest.approx(0.50)
    assert not scores_by_card_id[2].HasField("target_retention")
    target_map = rwkv_scheduler._rwkv_review_queue_target_map_for_deck(
        reviewer,
        100,
    )
    assert target_map is not None
    assert set(target_map) == {1}
    assert target_map[1] == pytest.approx(0.50)


def test_set_rwkv_review_queue_scores_replaces_cached_deck_scores() -> None:
    class Rpc:
        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            return b""

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=Rpc())))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )
    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        200,
        [(2, 0.75)],
    )

    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 100) is None
    deck_scores = rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 200)
    assert deck_scores is not None
    assert deck_scores[2] == pytest.approx(0.75)
    fresh_scores = rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer)
    assert fresh_scores[2] == pytest.approx(0.75)
    assert list(fresh_scores) == [2]


def test_clear_rwkv_review_queue_scores_clears_cached_deck_scores() -> None:
    class Rpc:
        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            return b""

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=Rpc())))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )
    rwkv_scheduler._clear_rwkv_review_queue_scores(reviewer, deck_id=200)

    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 100) is None
    assert rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer) == {}


def test_resident_invalidation_clears_scores_without_runtime_generation_change() -> (
    None
):
    class Backend:
        def state_generation(self) -> int:
            return 0

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.col.db = object()
    set_reviewer_backend(cast(Any, backend))
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    assert rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )
    assert rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer) == {
        1: pytest.approx(0.25)
    }

    rwkv_scheduler._invalidate_reviewer_backend_state(
        reviewer,
        reason="test resident loss",
    )

    assert rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer) == {}
    assert rpc.calls[-1] == {"deck_id": 0, "scores": []}


def test_unknown_resident_identity_keeps_patch_map_but_marks_it_stale() -> None:
    class Backend:
        def state_generation(self) -> int:
            return 0

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.col.db = object()
    set_reviewer_backend(cast(Any, backend))
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    assert rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )

    rwkv_scheduler._mark_reviewer_backend_identity_unknown(
        reviewer,
        reason="test answer",
    )

    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(
        reviewer,
        100,
    ) == {1: pytest.approx(0.25)}
    assert rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer) == {}
    assert len(rpc.calls) == 1


def test_queue_score_install_aborts_if_collection_swaps_during_request_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc_a = _RwkvQueueScoreRpc()
    rpc_b = _RwkvQueueScoreRpc()
    col_a = SimpleNamespace(_backend=rpc_a)
    col_b = SimpleNamespace(_backend=rpc_b)
    mw = SimpleNamespace(col=col_a)
    reviewer = SimpleNamespace(mw=mw)
    original = rwkv_scheduler._rwkv_score_request

    def swap_collection(
        scoped_reviewer: object,
        deck_id: int,
        scores: Sequence[tuple[int, float]],
        *,
        target_retentions_by_card_id: Mapping[int, float] | None = None,
    ) -> scheduler_pb2.RwkvReviewQueueScoresRequest:
        assert rwkv_scheduler._collection(scoped_reviewer) is col_a
        mw.col = col_b
        return original(
            scoped_reviewer,
            deck_id,
            scores,
            target_retentions_by_card_id=target_retentions_by_card_id,
        )

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_request", swap_collection)

    installed = rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
        collection_backend=rpc_a,
        collection=col_a,
        collection_owner=mw,
    )

    assert not installed
    assert rpc_a.calls == []
    assert rpc_b.calls == []
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}


def test_deck_count_install_aborts_if_collection_swaps_during_request_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rpc:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def set_rwkv_deck_count_scores(
            self,
            *,
            deck_id: int,
            scores: list[object],
        ) -> None:
            self.calls.append((deck_id, scores))

    rpc_a = Rpc()
    rpc_b = Rpc()
    col_a = SimpleNamespace(_backend=rpc_a)
    col_b = SimpleNamespace(_backend=rpc_b)
    mw = SimpleNamespace(col=col_a)
    reviewer = SimpleNamespace(mw=mw)
    original = rwkv_scheduler._rwkv_score_request

    def swap_collection(
        scoped_reviewer: object,
        deck_id: int,
        scores: Sequence[tuple[int, float]],
        *,
        target_retentions_by_card_id: Mapping[int, float] | None = None,
    ) -> scheduler_pb2.RwkvReviewQueueScoresRequest:
        assert rwkv_scheduler._collection(scoped_reviewer) is col_a
        mw.col = col_b
        return original(
            scoped_reviewer,
            deck_id,
            scores,
            target_retentions_by_card_id=target_retentions_by_card_id,
        )

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_request", swap_collection)

    installed = rwkv_scheduler._set_rwkv_deck_count_scores(
        reviewer,
        100,
        [(1, 0.25)],
        collection_backend=rpc_a,
        collection=col_a,
        collection_owner=mw,
    )

    assert not installed
    assert rpc_a.calls == []
    assert rpc_b.calls == []


def test_install_async_reviewer_queue_order_discards_stale_generation() -> None:
    class Backend:
        def state_generation(self) -> int:
            return 1

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=rwkv_scheduler.RwkvReviewQueueContext(
            collection_key=(1, 2),
            selected_deck_id=100,
            deck_id=100,
            deck_scope=(100,),
            days_elapsed=42,
            next_day_at=43 * 86_400,
            config_key="",
            review_input_generation=0,
            study_queue_generation=0,
        ),
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )
    previous_backend = set_reviewer_backend(Backend())
    try:
        installed = rwkv_scheduler.install_reviewer_queue_order_async_result(
            reviewer,
            result,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert installed is False
    assert rpc.calls == []


def test_async_scoring_skips_runtime_while_backend_execution_is_busy() -> None:
    class Backend:
        def __init__(self) -> None:
            self.runtime_calls: list[list[RwkvReviewInput]] = []

        def state_generation(self) -> int:
            return 0

        def predict_retrievability_inputs_from_warm_up_uncached(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[RwkvReviewPrediction]:
            self.runtime_calls.append(review_inputs)
            return [RwkvReviewPrediction(retrievability=0.5)]

    backend = Backend()
    set_reviewer_backend(cast(Any, backend))
    work = _rwkv_async_test_work(backend)
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_execution_lock() -> None:
        with rwkv_scheduler._reviewer_backend_execution_lock:
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    thread = threading.Thread(target=hold_execution_lock)
    thread.start()
    try:
        assert lock_acquired.wait(timeout=5)
        result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)
    finally:
        release_lock.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert result.state_generation == -1
    assert result.scores == ()
    assert backend.runtime_calls == []


def test_required_async_scoring_waits_for_backend_execution() -> None:
    class Backend:
        def __init__(self) -> None:
            self.runtime_calls: list[list[RwkvReviewInput]] = []

        def state_generation(self) -> int:
            return 0

        def predict_retrievability_inputs_from_warm_up_uncached(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[RwkvReviewPrediction]:
            self.runtime_calls.append(review_inputs)
            return [RwkvReviewPrediction(retrievability=0.5)]

    backend = Backend()
    set_reviewer_backend(cast(Any, backend))
    work = _rwkv_async_test_work(backend)
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    scoring_started = threading.Event()
    result: Future[rwkv_scheduler.RwkvReviewQueueOrderAsyncResult] = Future()

    def hold_execution_lock() -> None:
        with rwkv_scheduler._reviewer_backend_execution_lock:
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    def score() -> None:
        scoring_started.set()
        try:
            result.set_result(
                rwkv_scheduler.score_reviewer_queue_order_async_work(
                    work,
                    wait_for_backend=True,
                )
            )
        except BaseException as exc:
            result.set_exception(exc)

    lock_thread = threading.Thread(target=hold_execution_lock)
    lock_thread.start()
    assert lock_acquired.wait(timeout=5)
    score_thread = threading.Thread(target=score)
    score_thread.start()
    try:
        assert scoring_started.wait(timeout=5)
        assert not result.done()
    finally:
        release_lock.set()
    lock_thread.join(timeout=5)
    score_thread.join(timeout=5)

    assert not lock_thread.is_alive()
    assert not score_thread.is_alive()
    assert result.result(timeout=5).scores == ((1, pytest.approx(0.5)),)
    assert len(backend.runtime_calls) == 1


def test_async_scoring_discards_answer_epoch_invalidated_during_runtime() -> None:
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()

    class Backend:
        def __init__(self) -> None:
            self.runtime_calls = 0

        def state_generation(self) -> int:
            return 0

        def predict_retrievability_inputs_from_warm_up_uncached(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[RwkvReviewPrediction]:
            assert review_inputs
            self.runtime_calls += 1
            rwkv_scheduler._invalidate_reviewer_backend_state(
                reviewer,
                reason="test answer",
            )
            return [RwkvReviewPrediction(retrievability=0.5)]

    backend = Backend()
    set_reviewer_backend(cast(Any, backend))
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = (
        _rwkv_resident_identity()
    )
    work = replace(
        _rwkv_async_test_work(backend),
        resident_state_key=resident_key,
        resident_state_generation=0,
    )

    result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)

    assert backend.runtime_calls == 1
    assert result.state_generation == -1
    assert result.scores == ()
    assert resident_key not in rwkv_scheduler._reviewer_backend_warmup_states


def test_async_work_does_not_cross_backend_replacement_at_same_generation() -> None:
    class Backend:
        def __init__(self) -> None:
            self.runtime_calls: list[list[RwkvReviewInput]] = []
            self.cache_calls: list[object] = []

        def state_generation(self) -> int:
            return 0

        def predict_retrievability_inputs_from_warm_up_uncached(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[RwkvReviewPrediction]:
            self.runtime_calls.append(review_inputs)
            return [RwkvReviewPrediction(retrievability=0.5)]

        def cache_review_input_predictions(self, entries: object) -> None:
            self.cache_calls.append(entries)

    original = Backend()
    replacement = Backend()
    set_reviewer_backend(cast(Any, original))
    work = _rwkv_async_test_work(original)

    set_reviewer_backend(cast(Any, replacement))
    result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)

    assert result.state_generation == -1
    assert result.backend is original
    assert original.runtime_calls == []
    assert replacement.runtime_calls == []
    assert not rwkv_scheduler._cache_reviewer_queue_order_async_result_predictions(
        result
    )
    assert not rwkv_scheduler.install_reviewer_queue_order_async_result(
        SimpleNamespace(),
        result,
    )
    assert original.cache_calls == []
    assert replacement.cache_calls == []


def test_async_work_does_not_cross_same_backend_reset() -> None:
    class Backend:
        def __init__(self) -> None:
            self.runtime_calls = 0

        def state_generation(self) -> int:
            return 0

        def predict_retrievability_inputs_from_warm_up_uncached(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[RwkvReviewPrediction]:
            assert review_inputs
            self.runtime_calls += 1
            return [RwkvReviewPrediction(retrievability=0.5)]

    backend = Backend()
    set_reviewer_backend(cast(Any, backend))
    work = replace(
        _rwkv_async_test_work(backend),
        backend_assignment_generation=(
            rwkv_scheduler._reviewer_backend_assignment_generation
        ),
    )

    set_reviewer_backend(cast(Any, backend))
    result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)

    assert result.state_generation == -1
    assert backend.runtime_calls == 0


def test_async_prepare_rejects_backend_swap_after_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def state_generation(self) -> int:
            return 0

        def cached_review_input_predictions(self, inputs: object) -> object:
            raise AssertionError("swapped backend must not prepare cache work")

    original = Backend()
    replacement = Backend()
    set_reviewer_backend(cast(Any, original))
    reviewer = _rwkv_queue_reviewer(
        rpc=_RwkvQueueScoreRpc(),
        review_order=7,
    )

    def warm_up(_reviewer: object) -> bool:
        set_reviewer_backend(cast(Any, replacement))
        return True

    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_review",
        warm_up,
    )

    work = rwkv_scheduler.prepare_reviewer_queue_order_async_work(reviewer)

    assert work is None
    assert rwkv_scheduler._reviewer_backend is replacement


def test_async_queue_order_supports_stateless_backend_without_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def state_generation(self) -> int:
            return 0

        def cached_review_input_predictions(
            self,
            inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
        ) -> tuple[
            list[RwkvReviewPrediction | None],
            list[tuple[int, RwkvReviewPredictionRequest]],
            int,
        ]:
            return (
                [
                    RwkvReviewPrediction(
                        retrievability=review_input.identity.card_id / 10,
                    )
                    for _, review_input in inputs_by_index
                ],
                [],
                len(inputs_by_index),
            )

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        card_count=1,
    )
    review_input = _rwkv_review_input(card_id=1, note_id=10)
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, review_input)]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_deck_review_queue",
        lambda **kwargs: input_build,
    )

    backend = Backend()
    previous_backend = set_reviewer_backend(cast(Any, backend))
    try:
        work = rwkv_scheduler.prepare_reviewer_queue_order_async_work(reviewer)
        assert work is not None
        assert work.resident_state_key is None
        assert work.collection is reviewer.mw.col
        assert work.collection_backend is rpc

        result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)
        installed = rwkv_scheduler.install_reviewer_queue_order_async_result(
            reviewer,
            result,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert installed
    assert result.scores == ((1, pytest.approx(0.1)),)
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100


def test_async_reviewer_queue_order_scores_resident_inputs() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    inputs = (
        (2, _rwkv_review_input(card_id=2, note_id=20)),
        (3, _rwkv_review_input(card_id=3, note_id=30)),
    )
    work = rwkv_scheduler.RwkvReviewQueueOrderAsyncWork(
        context=rwkv_scheduler.RwkvReviewQueueContext(
            collection_key=(1, 2),
            selected_deck_id=100,
            deck_id=100,
            deck_scope=(100,),
            days_elapsed=42,
            next_day_at=43 * 86_400,
            config_key="",
            review_input_generation=0,
            study_queue_generation=0,
        ),
        deck_id=100,
        reason="review queue",
        batch_size=512,
        state_generation=0,
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={512: list(inputs)},
            loaded_rows=2,
            parsed_cards=2,
            cards_with_state=2,
            disabled_config_cards=0,
            eligible_cards=2,
            deck_configs=1,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        inputs_by_card_id=inputs,
        predictions=(None, None),
        requests_by_index=(),
        resident_inputs_by_index=((0, inputs[0][1]), (1, inputs[1][1])),
        cache_hits=0,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
    )

    result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)
    backend.cache_review_input_predictions(result.prediction_cache_entries)
    cached = backend.cached_retrievability_inputs_from_warm_up(
        [(0, inputs[0][1]), (1, inputs[1][1])]
    )

    assert result.scores == ((2, pytest.approx(0.20)), (3, pytest.approx(0.30)))
    assert result.runtime_requests == 2
    assert cached is not None
    cached_predictions, misses, cache_hits = cached
    assert [
        prediction.retrievability for prediction in cached_predictions if prediction
    ] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert misses == []
    assert cache_hits == 2

    overlapping_inputs = (
        *inputs,
        (4, _rwkv_review_input(card_id=4, note_id=40)),
    )
    overlapping_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: list(overlapping_inputs)},
        loaded_rows=3,
        parsed_cards=3,
        cards_with_state=3,
        disabled_config_cards=0,
        eligible_cards=3,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    collection_backend = object()
    collection = SimpleNamespace(_backend=collection_backend)
    scoped_reviewer = SimpleNamespace(mw=SimpleNamespace(col=collection))
    overlapping_work = rwkv_scheduler._rwkv_review_queue_async_work_from_input_build(
        reviewer=scoped_reviewer,
        deck_id=10,
        reason="parent prewarm",
        batch_size=512,
        state_generation=0,
        context=replace(
            work.context,
            collection_key=(id(collection), id(collection_backend)),
            deck_id=10,
        ),
        input_build=overlapping_build,
        warmup_elapsed_ms=0.0,
        build_start=time.monotonic(),
        fresh_for_backend_state=False,
    )

    assert overlapping_work is not None
    assert overlapping_work.cache_hits == 2
    assert len(overlapping_work.resident_inputs_by_index) == 1
    overlapping_result = rwkv_scheduler.score_reviewer_queue_order_async_work(
        overlapping_work
    )
    assert overlapping_result.scores == (
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
        (4, pytest.approx(0.40)),
    )
    assert runtime.resident_card_ids == [[2, 3], [4]]


def test_prewarm_reviewer_queue_score_cache_scores_parent_scope() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    cards = {
        1: _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        2: _rwkv_card(card_id=2, note_id=20, duration_millis=1234),
        3: _rwkv_card(card_id=3, note_id=30, duration_millis=1234),
    }
    cards[1].did = 100
    cards[2].did = 101
    cards[3].did = 200

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "queue = ?" in sql
            assert args == (2,)
            did_start = sql.index("did in (") + len("did in (")
            did_end = sql.index(")", did_start)
            deck_ids = {int(deck_id) for deck_id in sql[did_start:did_end].split(",")}
            return [
                card.id
                for card in cards.values()
                if card.did in deck_ids and card.queue == 2
            ]

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            id_start = sql.index("id in (") + len("id in (")
            id_end = sql.index(")", id_start)
            card_ids = {int(card_id) for card_id in sql[id_start:id_end].split(",")}
            data = json.dumps({"lrt": 4 * 86_400})
            return [
                (
                    card.id,
                    card.nid,
                    card.did,
                    0,
                    card.type,
                    card.queue,
                    card.due,
                    0,
                    card.ivl,
                    card.factor,
                    card.reps,
                    card.lapses,
                    data,
                )
                for card in cards.values()
                if card.id in card_ids
            ]

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def get(self, deck_id: int) -> dict[str, object]:
            names = {
                10: "Parent",
                100: "Parent::Child",
                101: "Parent::Child::Grandchild",
                200: "Parent::Sibling",
            }
            return {"id": deck_id, "name": names[deck_id]}

        def id_for_name(self, name: str, create: bool = True) -> int | None:
            return {"Parent": 10, "Parent::Child": 100}.get(name)

        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=deck_id) for deck_id in (10, 100, 101, 200)]

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            if deck_id == 100:
                return [100, 101]
            if deck_id == 10:
                return [10, 100, 101, 200]
            raise AssertionError(f"unexpected deck {deck_id}")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": False,
                "rwkvReviewInstantOrderEnabled": True,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    col = SimpleNamespace(
        _backend=_RwkvQueueScoreRpc(),
        db=DB(),
        decks=Decks(),
        sched=Scheduler(),
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    previous_backend = set_reviewer_backend(backend)
    try:
        rwkv_scheduler._reviewer_backend_warmup_states[(id(backend), id(col))] = None
        assert rwkv_scheduler._rwkv_score_prewarm_deck_ids(
            reviewer,
            include_parent_scope=False,
        ) == [100]
        prewarm_reviewer_queue_score_cache(reviewer, reason="test")
    finally:
        set_reviewer_backend(previous_backend)

    assert runtime.retrievability_card_ids == [[1, 2], [1, 2, 3]]
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}


def test_async_score_prewarm_releases_collection_while_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    collection_flags: list[bool] = []
    cached_deck_ids: list[int] = []
    finished: list[bool] = []

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> SimpleNamespace | None:
        events.append(("prepare", deck_id))
        if deck_id == 100:
            return None
        return SimpleNamespace(
            deck_id=deck_id,
            input_build=SimpleNamespace(searched_rows=3),
        )

    def score(work: SimpleNamespace) -> SimpleNamespace:
        events.append(("score", work.deck_id))
        return SimpleNamespace(
            deck_id=work.deck_id,
            scores=((1, 0.5), (2, 0.6)),
        )

    def cache_predictions(
        reviewer: object,
        result: SimpleNamespace,
    ) -> bool:
        cached_deck_ids.append(result.deck_id)
        return True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            collection_flags.append(uses_collection)
            future: Future[object] = Future()
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)
            on_done(future)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(rwkv_scheduler, "score_reviewer_queue_order_async_work", score)
    monkeypatch.setattr(
        rwkv_scheduler,
        "cache_reviewer_queue_order_async_result_predictions",
        cache_predictions,
    )

    rwkv_scheduler._prewarm_rwkv_review_scores_for_decks_async(
        SimpleNamespace(),
        [100, 10],
        reason="test",
        taskman=Taskman(),
        on_done=lambda: finished.append(True),
    )

    assert events == [("prepare", 100), ("prepare", 10), ("score", 10)]
    assert collection_flags == [True, True, False, True]
    assert cached_deck_ids == [10]
    assert finished == [True]


def test_deck_browser_count_scopes_are_disjoint_and_prioritize_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = SimpleNamespace(
        children=[
            SimpleNamespace(
                deck_id=10,
                children=[SimpleNamespace(deck_id=11, children=[])],
            ),
            SimpleNamespace(
                deck_id=20,
                children=[SimpleNamespace(deck_id=21, children=[])],
            ),
        ]
    )
    enabled_decks = {10, 21}
    monkeypatch.setattr(rwkv_scheduler, "_current_deck_id", lambda reviewer: 11)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda reviewer, deck_id: {
            "rwkvReviewEnabled": False,
            "rwkvReviewInstantOrderEnabled": deck_id in enabled_decks,
            "reviewOrder": 0,
        },
    )

    assert rwkv_scheduler.deck_browser_rwkv_count_scope_ids(
        SimpleNamespace(),
        tree,
    ) == (10, 21)


def test_deck_browser_counts_score_off_collection_and_update_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    collection_flags: list[bool] = []
    updates: list[tuple[int, int | None]] = []
    installed_scores: list[int] = []
    due_tree_calls = 0
    context = SimpleNamespace(
        review_input_generation=0,
        study_queue_generation=0,
    )

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> SimpleNamespace | None:
        events.append(("prepare", deck_id))
        if deck_id == 20:
            return None
        return SimpleNamespace(deck_id=deck_id, context=context)

    def score(work: SimpleNamespace) -> SimpleNamespace:
        events.append(("score", work.deck_id))
        return SimpleNamespace(
            deck_id=work.deck_id,
            context=work.context,
            state_generation=0,
            scores=((work.deck_id, 0.5),),
            target_retentions_by_card_id={},
        )

    def install(
        reviewer: object,
        deck_id: int,
        scores: Sequence[tuple[int, float]],
        *,
        target_retentions_by_card_id: Mapping[int, float],
        **_kwargs: object,
    ) -> bool:
        installed_scores.append(deck_id)
        return True

    class Scheduler:
        def deck_due_tree(self) -> SimpleNamespace:
            nonlocal due_tree_calls
            due_tree_calls += 1
            return SimpleNamespace(version=due_tree_calls)

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            collection_flags.append(uses_collection)
            future: Future[object] = Future()
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)
            on_done(future)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(rwkv_scheduler, "score_reviewer_queue_order_async_work", score)
    monkeypatch.setattr(rwkv_scheduler, "_set_rwkv_deck_count_scores", install)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend_state_generation", lambda: 0)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_queue_context",
        lambda reviewer, deck_id: context,
    )
    mw = SimpleNamespace(
        col=SimpleNamespace(sched=Scheduler()),
        taskman=Taskman(),
    )

    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10, 20, 30],
        should_continue=lambda: True,
        on_update=lambda deck_id, tree: updates.append(
            (deck_id, tree.version if tree is not None else None)
        ),
    )

    assert events == [
        ("prepare", 10),
        ("score", 10),
        ("prepare", 20),
        ("prepare", 30),
        ("score", 30),
    ]
    assert collection_flags == [True, False, True, True, True, False, True]
    assert installed_scores == [10, 30]
    # deck 20 had nothing to score: its count stays pending
    assert updates == [(10, 1), (30, 2)]


def test_deck_browser_count_prepare_skips_work_cancelled_while_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[tuple[Callable[[], object], Callable[[Future[object]], None]]] = []
    prepared_deck_ids: list[int] = []
    finished: list[bool] = []
    active = True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            assert uses_collection
            queued.append((task, on_done))

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> None:
        prepared_deck_ids.append(deck_id)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    mw = SimpleNamespace(col=SimpleNamespace(), taskman=Taskman())
    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10],
        should_continue=lambda: active,
        on_update=lambda _deck_id, _tree: pytest.fail("cancelled work was installed"),
        on_done=finished.append,
    )

    assert len(queued) == 1
    active = False
    task, on_done = queued.pop()
    future: Future[object] = Future()
    future.set_result(task())
    on_done(future)

    assert prepared_deck_ids == []
    assert finished == [True]


def test_deck_browser_count_prepare_failure_after_cancellation_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[tuple[Callable[[], object], Callable[[Future[object]], None]]] = []
    finished: list[bool] = []
    active = True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            assert uses_collection
            queued.append((task, on_done))

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> None:
        nonlocal active
        active = False
        raise RuntimeError("CollectionNotOpen")

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(
        rwkv_scheduler.logger,
        "exception",
        lambda *_args, **_kwargs: pytest.fail(
            "cancelled failure was logged as an error"
        ),
    )
    mw = SimpleNamespace(col=SimpleNamespace(), taskman=Taskman())
    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10],
        should_continue=lambda: active,
        on_update=lambda _deck_id, _tree: pytest.fail("cancelled work was installed"),
        on_done=finished.append,
    )

    task, on_done = queued.pop()
    future: Future[object] = Future()
    try:
        future.set_result(task())
    except Exception as error:
        future.set_exception(error)
    on_done(future)

    assert finished == [True]


# Pins spec/scheduling.md#sched.rwkv-instant-waits
def test_deck_browser_count_failure_keeps_the_review_count_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished: list[bool] = []

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            future: Future[object] = Future()
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)
            on_done(future)

    def prepare(reviewer: object, *, deck_id: int, reason: str) -> None:
        if deck_id == 10:
            raise RuntimeError("scoring failed")
        return None

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(rwkv_scheduler.logger, "exception", lambda *a, **k: None)
    mw = SimpleNamespace(col=SimpleNamespace(), taskman=Taskman())

    for deck_ids in ([10], [20]):
        rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
            mw,
            deck_ids,
            should_continue=lambda: True,
            on_update=lambda _deck_id, _tree: pytest.fail("no count to show"),
            on_done=finished.append,
        )

    # a failure (10) and nothing to score (20) both keep "…"
    assert finished == [False, False]


def test_selecting_deck_cancels_counts_and_defers_overview_until_after_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.deckbrowser import DeckBrowser

    events: list[str] = []
    timers: list[Callable[[], None]] = []

    class Operation:
        def __init__(self) -> None:
            self.callback: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> Operation:
            self.callback = callback
            return self

        def run_in_background(self, *, initiator: object) -> None:
            assert initiator is browser
            assert self.callback is not None
            self.callback(object())
            events.append("operation hooks")

    monkeypatch.setattr(
        "aqt.deckbrowser.set_current_deck",
        lambda *, parent, deck_id: Operation(),
    )
    browser = DeckBrowser.__new__(DeckBrowser)
    browser._rwkv_count_generation = 3
    browser.mw = SimpleNamespace(
        onOverview=lambda: events.append("overview"),
        progress=SimpleNamespace(
            single_shot=lambda delay, callback: timers.append(callback)
        ),
    )

    browser.set_current_deck(DeckId(10))

    assert browser._rwkv_count_generation == 4
    assert events == ["operation hooks"]
    assert len(timers) == 1
    timers.pop()()
    assert events == ["operation hooks", "overview"]


def test_deck_browser_pending_rwkv_scopes_render_review_counts_as_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    from aqt.deckbrowser import DeckBrowser

    child = SimpleNamespace(
        deck_id=11,
        new_count=3,
        learn_count=4,
        review_count=5,
        review_uncapped=5,
        children=[],
    )
    pending_scope = SimpleNamespace(
        deck_id=10,
        new_count=1,
        learn_count=2,
        review_count=60,
        review_uncapped=60,
        children=[child],
    )
    ready_scope = SimpleNamespace(
        deck_id=20,
        new_count=6,
        learn_count=7,
        review_count=8,
        review_uncapped=8,
        children=[],
    )
    tree = SimpleNamespace(children=[pending_scope, ready_scope])
    scripts: list[str] = []
    browser = DeckBrowser.__new__(DeckBrowser)
    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    browser.web = SimpleNamespace(eval=scripts.append)
    browser._render_data = SimpleNamespace(tree=tree)
    browser._rwkv_pending_deck_ids = browser._deck_ids_in_rwkv_scopes(tree, [10])

    browser._render_rwkv_deck_counts()

    assert browser._rwkv_pending_deck_ids == {10, 11}
    assert "[[10, 1, 2, null], [11, 3, 4, null], [20, 6, 7, 8]]" in scripts[0]


def test_deck_browser_keeps_pending_counts_when_cache_load_is_deferred() -> None:
    from aqt.deckbrowser import DeckBrowser

    renders: list[set[int]] = []
    browser = DeckBrowser.__new__(DeckBrowser)
    browser.mw = SimpleNamespace(state="deckBrowser")
    browser._rwkv_count_generation = 2
    browser._rwkv_pending_deck_ids = {10, 11}
    browser._render_rwkv_deck_counts = lambda: renders.append(
        set(browser._rwkv_pending_deck_ids)
    )

    browser._finish_rwkv_count_refresh(2, clear_pending=False)
    browser._finish_rwkv_count_refresh(2, clear_pending=True)

    assert renders == [{10, 11}, set()]


def test_deck_browser_reschedule_all_decks_uses_global_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.deckbrowser import DeckBrowser

    calls: list[tuple[object, int | None]] = []

    def reschedule(mw: object, *, deck_id: int | None = None) -> None:
        calls.append((mw, deck_id))

    monkeypatch.setattr(
        rwkv_scheduler,
        "reschedule_rwkv_review_cards_with_progress",
        reschedule,
    )
    mw = object()
    browser = DeckBrowser.__new__(DeckBrowser)
    browser.mw = mw

    browser._reschedule_all_decks_with_rwkv_curve()

    assert calls == [(mw, None)]


def test_overview_renders_pending_rwkv_review_count_as_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.overview import Overview
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    scheduler = SimpleNamespace(
        counts=lambda: (1, 2, 4_000),
        deck_due_counts=lambda _deck_id: SimpleNamespace(
            new_count=1,
            learn_count=2,
            review_count=4_000,
        ),
    )
    overview = Overview.__new__(Overview)
    overview.mw = SimpleNamespace(
        col=SimpleNamespace(
            sched=scheduler,
            decks=SimpleNamespace(get_current_id=lambda: 10),
            v3_scheduler=lambda: True,
        ),
        button=lambda *args, **kwargs: "",
        advanced_ui=lambda: True,
    )
    overview._rwkv_counts_pending = True

    table = overview._table()

    assert "<span class=review-count>…</span>" in table
    assert "4000" not in table


def _pending_overview(
    monkeypatch: pytest.MonkeyPatch, *, model: bool
) -> tuple[object, list[str]]:
    from aqt.overview import Overview

    monkeypatch.setattr(
        "aqt.overview.tr",
        SimpleNamespace(
            qt_misc_rwkv_instant_scores_pending=lambda: "WAITING",
            qt_misc_rwkv_model_not_found=lambda: "NO MODEL",
        ),
    )
    monkeypatch.setattr(rwkv_scheduler, "rwkv_model_available", lambda: model)
    pages: list[str] = []
    overview = Overview.__new__(Overview)
    overview.mw = SimpleNamespace(
        col=SimpleNamespace(
            sched=SimpleNamespace(_is_finished=lambda: True),
            decks=SimpleNamespace(current=lambda: {"name": "Deck", "dyn": 0}),
        ),
    )
    overview.web = SimpleNamespace(
        stdHtml=lambda body, **kwargs: pages.append(body),
        load_sveltekit_page=lambda page: pages.append(f"sveltekit:{page}"),
    )
    overview._table = lambda: "TABLE"
    overview._desc = lambda deck: ""
    overview._rwkv_counts_pending = True
    return overview, pages


# Pins spec/scheduling.md#sched.rwkv-instant-waits
def test_overview_waits_for_rwkv_instant_instead_of_congratulating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overview, pages = _pending_overview(monkeypatch, model=True)
    overview._renderPage()
    assert "TABLE" in pages[0] and "WAITING" in pages[0]

    overview, pages = _pending_overview(monkeypatch, model=False)
    overview._renderPage()
    assert "NO MODEL" in pages[0]

    overview, pages = _pending_overview(monkeypatch, model=True)
    overview._rwkv_counts_pending = False
    overview._renderPage()
    assert pages == ["sveltekit:congrats"]


# Pins spec/scheduling.md#sched.rwkv-instant-waits
def test_overview_retries_while_rwkv_instant_scores_are_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.overview import Overview

    timers: list[tuple[int, Callable[[], None]]] = []
    refreshes: list[int] = []
    monkeypatch.setattr(rwkv_scheduler, "rwkv_model_available", lambda: True)
    overview = Overview.__new__(Overview)
    overview.mw = SimpleNamespace(
        state="overview",
        progress=SimpleNamespace(
            single_shot=lambda delay, callback, *args: timers.append((delay, callback))
        ),
    )
    overview.refresh = lambda: refreshes.append(1)
    overview._rwkv_counts_pending = True
    overview._rwkv_retry_scheduled = False

    overview._retry_rwkv_counts()
    overview._retry_rwkv_counts()
    assert [delay for delay, _ in timers] == [2000]
    timers[0][1]()
    assert refreshes == [1]

    # no retry once the scores came, and none without a model
    overview._rwkv_counts_pending = False
    overview._retry_rwkv_counts()
    timers[1][1]()
    assert refreshes == [1]
    monkeypatch.setattr(rwkv_scheduler, "rwkv_model_available", lambda: False)
    overview._rwkv_retry_scheduled = False
    overview._retry_rwkv_counts()
    assert len(timers) == 2

    col = SimpleNamespace(
        sched=SimpleNamespace(
            get_queued_cards_without_states=lambda fetch_limit: SimpleNamespace(
                rwkv_scores_pending=fetch_limit == 0
            )
        )
    )
    assert rwkv_scheduler.rwkv_review_scores_pending(col)


@pytest.mark.parametrize("enforce_grade_order", [True, False])
def test_backend_review_input_rows_preserve_grade_order(
    enforce_grade_order: bool,
) -> None:
    object_input = rwkv_scheduler._rwkv_review_input_from_backend_row(
        SimpleNamespace(card_id=1, enforce_grade_order=enforce_grade_order)
    )
    proto_input = rwkv_scheduler._rwkv_review_input_from_backend_proto_row(
        scheduler_pb2.RwkvReviewInputRowsForCardsResponse.Row(
            card_id=1,
            enforce_grade_order=enforce_grade_order,
        )
    )

    assert object_input is not None
    assert object_input.enforce_grade_order is enforce_grade_order
    assert proto_input.enforce_grade_order is enforce_grade_order


def test_backend_review_input_rows_default_to_enforced_grade_order() -> None:
    object_input = rwkv_scheduler._rwkv_review_input_from_backend_row(
        SimpleNamespace(card_id=1)
    )
    proto_input = rwkv_scheduler._rwkv_review_input_from_backend_proto_row(
        scheduler_pb2.RwkvReviewInputRowsForCardsResponse.Row(card_id=1)
    )

    assert object_input is not None
    assert object_input.enforce_grade_order is True
    assert proto_input.enforce_grade_order is True


def test_backend_row_failure_logs_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Backend:
        def rwkv_review_input_rows_for_cards_raw(self, message: bytes) -> bytes:
            raise RuntimeError("backend row failure")

    with caplog.at_level("DEBUG", logger="aqt.rwkv_scheduler"):
        response = rwkv_scheduler._rwkv_review_input_rows_backend_response(
            Backend(),
            card_ids=[1],
            include_suspended_review=False,
            include_new_cards=False,
        )

    assert response is None
    record = next(
        record
        for record in caplog.records
        if record.message == "failed to load RWKV review input rows from backend"
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


def test_rwkv_resolved_preset_cache_invalidates_selected_cards() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }
    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }

    rwkv_scheduler._invalidate_resolved_preset_id_cache(reviewer, card_ids=[1])

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }
    assert rpc.preset_id_calls == [[1, 2], [1]]


def test_prepare_stats_retrievability_scores_ignores_review_order() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(
                retrievability={1: 0.75, 3: 0.25, 4: 0.55, 5: 0.71}[card.id]
            )

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            if deck_id == 100:
                return {"id": 1000, "rwkvReviewEnabled": True}
            if deck_id == 200:
                return {"id": 2000, "rwkvReviewEnabled": False}
            if deck_id == 300:
                return {
                    "id": 3000,
                    "other": {"jschoreels.fsrs": {"rwkv_review_enabled": True}},
                }
            if deck_id == 400:
                return {
                    "id": 4000,
                    "other": {"jschoreels.rwkv": {"rwkv_review_enabled": True}},
                }
            raise AssertionError(f"unexpected deck {deck_id}")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            raise AssertionError("stats graph scores should bulk-load card rows")

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type = 4" in sql
                return []
            assert "from cards" in sql
            assert "id in (1,2,3,4,5)" in sql
            assert "type = 2 and queue in (2, -1)" in sql
            assert "type = 1 and queue in (1, 3)" in sql
            assert "type = 3 and queue in (1, 3)" in sql
            return [
                (1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (2, 20, 200, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (3, 30, 300, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (4, 40, 400, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (5, 50, 100, 0, 2, -1, 50, 0, 4, 2500, 5, 1, ""),
            ]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search == "rated:7"
            assert order is False
            return [1, 2, 3, 4, 5]

        def get_card(self, card_id: int) -> SimpleNamespace:
            raise AssertionError("stats graph scores should bulk-load card rows")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 3, 4, 5]
    assert rpc.preset_id_calls == [[1, 3, 4, 5]]
    assert rpc.calls == []
    assert len(rpc.stats_calls) == 1
    assert rpc.stats_calls[0]["search"] == "rated:7"
    scores = rpc.stats_calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.75)),
        (3, pytest.approx(0.25)),
        (4, pytest.approx(0.55)),
        (5, pytest.approx(0.71)),
    ]


@pytest.mark.parametrize(
    ("search", "instant_due", "curve_due"),
    [
        ("deck:current is:rwkv:due", True, False),
        ("deck:current -is:rwkv:due", True, False),
        ("deck:current IS:RWKV-CURVE:DUE", False, True),
        ("tag:prefix-is:rwkv:due", False, False),
    ],
)
def test_rwkv_due_search_detection(
    search: str,
    instant_due: bool,
    curve_due: bool,
) -> None:
    assert rwkv_scheduler._search_uses_rwkv_instant_due(search) is instant_due
    assert rwkv_scheduler._search_uses_rwkv_curve_due(search) is curve_due


@pytest.mark.parametrize(
    (
        "search",
        "order",
        "prepare_instant_due",
        "prepare_curve_due",
        "prepare_curve_retrievability",
    ),
    [
        (
            '"deck:Japan::1. Vocabulary" prop:rwkv:r<0.9',
            FilteredDeckConfig.SearchTerm.DUE,
            False,
            False,
            False,
        ),
        (
            '"deck:Japan::1. Vocabulary" prop:rwkv-curve:r<0.9',
            FilteredDeckConfig.SearchTerm.DUE,
            False,
            False,
            True,
        ),
        (
            '"deck:Japan::1. Vocabulary"',
            FilteredDeckConfig.SearchTerm.RETRIEVABILITY_ASCENDING,
            False,
            False,
            False,
        ),
        (
            '"deck:Japan::1. Vocabulary" is:rwkv:due',
            FilteredDeckConfig.SearchTerm.DUE,
            True,
            False,
            False,
        ),
        (
            '"deck:Japan::1. Vocabulary" is:rwkv-curve:due',
            FilteredDeckConfig.SearchTerm.DUE,
            False,
            True,
            False,
        ),
    ],
)
def test_filtered_deck_retrievability_prepares_rwkv_candidate_scores(
    monkeypatch: pytest.MonkeyPatch,
    search: str,
    order: int,
    prepare_instant_due: bool,
    prepare_curve_due: bool,
    prepare_curve_retrievability: bool,
) -> None:
    class Collection:
        def __init__(self) -> None:
            self.build_calls: list[tuple[tuple[str, ...], str]] = []

        def build_search_string(self, *searches: str, joiner: str) -> str:
            self.build_calls.append((searches, joiner))
            return "combined search"

    col = Collection()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    config = FilteredDeckConfig(
        search_terms=[
            FilteredDeckConfig.SearchTerm(
                search=search,
                limit=9999,
                order=order,
            )
        ]
    )
    prepared: list[tuple[object, str, bool, bool, bool]] = []

    def prepare(
        reviewer: object,
        search: str,
        *,
        warm_up_if_needed: bool = False,
        prepare_instant_due: bool = False,
        prepare_curve_due: bool = False,
        prepare_curve_retrievability: bool = False,
        prepare_instant_retrievability: bool = False,
        publish_as: str | None = None,
        reuse_kept_scores: bool = True,
    ) -> rwkv_scheduler.RwkvStatsPreparationStatus:
        assert warm_up_if_needed
        # the deck's own map (spec sched.filtered-deck-one-algorithm)
        assert publish_as == rwkv_scheduler.FILTERED_DECK_RWKV_SCORES_SEARCH
        # scored now, never an earlier map (spec sched.rwkv-r-freshness)
        assert reuse_kept_scores is False
        # outside RWKV-Curve, a retrievability order reads the rating head
        assert prepare_instant_retrievability == (
            order == FilteredDeckConfig.SearchTerm.RETRIEVABILITY_ASCENDING
        )
        prepared.append(
            (
                reviewer,
                search,
                prepare_instant_due,
                prepare_curve_due,
                prepare_curve_retrievability,
            )
        )
        return rwkv_scheduler.RwkvStatsPreparationStatus.READY

    monkeypatch.setattr(
        rwkv_scheduler,
        "prepare_stats_retrievability_scores",
        prepare,
    )

    status = prepare_filtered_deck_retrievability_scores(reviewer, config)

    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    assert col.build_calls == [((search,), "OR")]
    assert prepared == [
        (
            reviewer,
            "combined search",
            prepare_instant_due,
            prepare_curve_due,
            prepare_curve_retrievability,
        )
    ]


def test_filtered_deck_key_matches_the_rust_build() -> None:
    """The Python preparation and the Rust build name the deck's own map the
    same way (rslib `FILTERED_DECK_RWKV_SCORES_SEARCH`)."""

    source = (
        Path(__file__).resolve().parents[2] / "rslib/src/scheduler/filtered/mod.rs"
    ).read_text(encoding="utf-8")
    assert (
        'pub const FILTERED_DECK_RWKV_SCORES_SEARCH: &str = "\\u{0}filtered-deck";'
        in source
    )
    assert rwkv_scheduler.FILTERED_DECK_RWKV_SCORES_SEARCH == "\x00filtered-deck"


@pytest.mark.parametrize(
    ("algorithm", "curve", "rating_head"),
    [("rwkvCurve", True, False), ("rwkvInstant", False, True)],
)
def test_filtered_deck_retrievability_order_scores_its_own_cards(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    curve: bool,
    rating_head: bool,
) -> None:
    """Pins spec/scheduling.md#sched.filtered-deck-one-algorithm: a
    retrievability order scores the deck's own cards with the collection's
    algorithm (RWKV-Curve's stored curves, or RWKV-Instant's rating head, not
    both) and publishes them under the deck's own name, never borrowing
    another search's map; when that fails, the deck's map is emptied."""

    class Collection:
        def build_search_string(self, *searches: str, joiner: str) -> str:
            return "combined search"

        def get_config(self, key: str, default: object = None) -> object:
            return algorithm if key == "schedulingAlgorithm" else default

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection()))
    config = FilteredDeckConfig(
        search_terms=[
            FilteredDeckConfig.SearchTerm(
                search="deck:Japan",
                limit=300,
                order=FilteredDeckConfig.SearchTerm.RETRIEVABILITY_ASCENDING,
            )
        ]
    )
    calls: list[dict[str, Any]] = []
    status = [rwkv_scheduler.RwkvStatsPreparationStatus.READY]

    def prepare(
        _reviewer: object, search: str, **kwargs: Any
    ) -> rwkv_scheduler.RwkvStatsPreparationStatus:
        calls.append({"search": search, **kwargs})
        return status[0]

    published: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(rwkv_scheduler, "prepare_stats_retrievability_scores", prepare)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_set_rwkv_stats_graph_scores",
        lambda _reviewer, search, scores, **_kwargs: published.append(
            (search, list(scores))
        ),
    )

    assert prepare_filtered_deck_retrievability_scores(reviewer, config) == status[0]
    (call,) = calls
    assert call["search"] == "combined search"
    assert call["publish_as"] == rwkv_scheduler.FILTERED_DECK_RWKV_SCORES_SEARCH
    assert call["prepare_curve_retrievability"] is curve
    assert call["prepare_instant_retrievability"] is rating_head
    assert published == []

    status[0] = rwkv_scheduler.RwkvStatsPreparationStatus.UNAVAILABLE
    prepare_filtered_deck_retrievability_scores(reviewer, config)
    assert published == [(rwkv_scheduler.FILTERED_DECK_RWKV_SCORES_SEARCH, [])]


def test_filtered_deck_fsrs_retrievability_does_not_prepare_rwkv_scores() -> None:
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=object()))
    config = FilteredDeckConfig(
        search_terms=[
            FilteredDeckConfig.SearchTerm(
                search='"deck:Japan::1. Vocabulary" prop:r<0.9',
                limit=9999,
                order=FilteredDeckConfig.SearchTerm.DUE,
            )
        ]
    )

    assert prepare_filtered_deck_retrievability_scores(reviewer, config) is None


def test_filtered_deck_retrievability_warms_cold_rwkv_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(db=object())),
    )
    set_reviewer_backend(object())  # type: ignore[arg-type]
    warmups: list[object] = []

    def warm_up(candidate: object) -> bool:
        warmups.append(candidate)
        return True

    monkeypatch.setattr(rwkv_scheduler, "_warm_up_reviewer_backend", warm_up)

    assert rwkv_scheduler._prepare_reviewer_backend_for_filtered_deck(reviewer)
    assert warmups == [reviewer]


def test_filtered_deck_curve_due_uses_current_curve_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due_input = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        current_elapsed_days=7,
        target_retentions=(0.8, 0.8, 0.8, 0.8),
    )
    future_input = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        current_elapsed_days=2,
        target_retentions=(0.9, 0.9, 0.9, 0.9),
    )
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, due_input), (2, future_input)]},
        loaded_rows=2,
        parsed_cards=2,
        cards_with_state=2,
        disabled_config_cards=0,
        eligible_cards=2,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_search",
        lambda **_kwargs: input_build,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_curve_enabled_input_build",
        lambda _reviewer, build: build,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_predictions_for_inputs",
        lambda *_args, **_kwargs: [
            RwkvReviewPrediction(
                retrievability=0.7,
                curve_retrievability=0.6,
                current_interval=5,
            ),
            RwkvReviewPrediction(
                retrievability=0.9,
                curve_retrievability=0.8,
                current_interval=5,
            ),
        ],
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_fresh_rwkv_review_queue_score_map",
        lambda _reviewer: {},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_filtered_deck_intervening_reviews_by_card_id",
        lambda _reviewer, _inputs: {1: 3},
    )
    # the curve value is the stored curve (spec ui.rwkv-curve-r-stored-curve)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_stored_curve_retrievabilities_for_inputs",
        lambda inputs, **_kwargs: [
            (card_id, {1: 0.55, 2: 0.85}[card_id]) for card_id, _ in inputs
        ],
    )

    backend = SimpleNamespace(cached_review_input_predictions=lambda _inputs: None)
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        result = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="is:rwkv-curve:due",
            prepare_instant_due=True,
            prepare_curve_due=True,
            prepare_curve_retrievability=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert result is not None
    assert result.scores == [(1, 0.7), (2, 0.9)]
    assert result.curve_scores == [(1, 0.55), (2, 0.85)]
    assert result.target_retentions_by_card_id == {1: 0.8, 2: 0.9}
    assert result.intervening_reviews_by_card_id == {1: 3}
    assert result.curve_due_card_ids == frozenset({1})


def test_stats_score_transport_includes_due_metadata() -> None:
    class Backend:
        def __init__(self) -> None:
            self.scores: list[object] = []

        def set_rwkv_stats_graph_scores(
            self,
            *,
            search: str,
            scores: list[object],
        ) -> None:
            assert search == "is:rwkv:due"
            self.scores = scores

    backend = Backend()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(_backend=backend))
    )
    rwkv_scheduler._set_rwkv_stats_graph_scores(
        reviewer,
        "is:rwkv:due",
        [(1, 0.7), (2, 0.9)],
        target_retentions_by_card_id={1: 0.8},
        intervening_reviews_by_card_id={1: 3},
        curve_due_card_ids={2},
        curve_retrievabilities_by_card_id={1: 0.6},
    )

    first, second = backend.scores
    assert getattr(first, "target_retention") == pytest.approx(0.8)
    assert getattr(first, "intervening_reviews") == 3
    assert getattr(first, "curve_retrievability") == pytest.approx(0.6)
    assert not first.HasField("curve_due")
    assert second.HasField("curve_due")
    assert getattr(second, "curve_due") is True


def test_stats_graph_scores_build_direct_review_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.64 for _ in requests]

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewFirstReviewElapsedFromCardCreation": True,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(),
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_stats_graph_scheduling_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stats scoring should build RWKV inputs directly")
        ),
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer, card_ids=[1]
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.64))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.identity == RwkvReviewIdentity(
        card_id=1,
        note_id=10,
        deck_id=100,
        preset_id=1000,
    )
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "review"
    assert review_input.current_elapsed_days == 39
    assert review_input.target_retentions == (0.86, 0.86, 0.86, 0.86)


def test_stats_graph_scores_build_direct_new_card_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    card_id = (now - 90_000) * 1000

    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.83 for _ in requests]

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewFirstReviewElapsedFromCardCreation": True,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=now,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            return [(card_id, 10, 100, 0, 0, 0, 50, 0, 0, 0, 0, 0, "")]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(),
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[card_id],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(card_id, pytest.approx(0.83))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_stats_graph_scores_backend_card_rows_can_include_new_cards() -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.83 for _ in requests]

    class Rpc:
        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            assert card_ids == [1]
            assert include_suspended_review is True
            assert include_disabled_decks is False
            assert include_new_cards is True
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=0,
                        card_queue=0,
                        card_due=50,
                        interval_days=0,
                        ease_factor=0,
                        reps=0,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="new",
                        current_elapsed_days=1,
                        current_elapsed_seconds=90_000,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Rpc(),
                db=object(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1],
            include_new_cards=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.83))]
    assert len(runtime.query_inputs) == 1
    assert runtime.query_inputs[0].current_normal_state_kind == "new"


def test_stats_graph_scores_use_backend_review_input_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.71 for _ in requests]

    class Rpc:
        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            assert card_ids == [1]
            assert include_suspended_review is True
            assert include_disabled_decks is False
            assert include_new_cards is False
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            raise AssertionError("backend row path should not query cards directly")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Rpc(),
                db=DB(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_stats_graph_scheduling_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend rows should build RWKV inputs directly")
        ),
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.71))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.identity == RwkvReviewIdentity(
        card_id=1,
        note_id=10,
        deck_id=100,
        preset_id=rwkv_scheduler._stable_preset_id("addon-preset"),
    )
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "review"
    assert review_input.current_elapsed_days == 39
    assert review_input.current_elapsed_seconds is None
    assert review_input.target_retentions == (0.86, 0.86, 0.86, 0.86)


def test_prepare_stats_uses_backend_search_review_input_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.search_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            self.search_row_calls.append(
                {
                    "search": search,
                    "include_suspended_review": include_suspended_review,
                    "include_disabled_decks": include_disabled_decks,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            raise AssertionError("stats graph should use backend search rows")

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.search_row_calls == [
        {
            "search": "rated:7",
            "include_suspended_review": True,
            "include_disabled_decks": False,
        }
    ]
    assert len(rpc.stats_calls) == 1
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.69)),
    ]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]


# Pins spec/scheduling.md#sched.rwkv-r-freshness: the study queue's scores
# carry no time, so a Stats map scores every card now instead of reusing them
def test_prepare_stats_scores_every_card_now_not_from_the_queue_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            assert search == "rated:7"
            assert include_suspended_review is True
            assert include_disabled_decks is False
            rows = []
            for card_id in (1, 2):
                rows.append(
                    SimpleNamespace(
                        card_id=card_id,
                        note_id=card_id * 10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                )
            return SimpleNamespace(
                rows=rows,
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    try:
        rwkv_scheduler._set_rwkv_review_queue_scores(reviewer, 100, [(1, 0.42)])
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1,
        2,
    ]
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.69)),
        (2, pytest.approx(0.69)),
    ]


def test_prepare_stats_ignores_stale_review_queue_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    resident_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert resident_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    try:
        rwkv_scheduler._set_rwkv_review_queue_scores(
            reviewer,
            100,
            [(1, 0.42)],
            fresh_for_backend_state=False,
        )
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.69)),
    ]


def test_stats_graph_scores_filters_disabled_decks_in_sql() -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.73 for _ in requests]

    class Decks:
        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=100), SimpleNamespace(id=200)]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": deck_id == 100,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Rpc:
        def __init__(self) -> None:
            self.preset_id_calls: list[list[int]] = []

        def get_fsrs_preset_ids_for_cards(
            self,
            card_ids: list[int],
        ) -> SimpleNamespace:
            self.preset_id_calls.append(card_ids)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(card_id=card_id, preset_id="1000")
                    for card_id in card_ids
                ]
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            assert "id in (1,2)" in sql
            assert "case when odid != 0 then odid else did end" in sql
            assert "in (100)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1, 2],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.73))]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]
    assert rpc.preset_id_calls == [[1]]


def test_rwkv_reschedule_items_use_current_interval_and_s90() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("reschedule should batch predictions")

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            return [
                RwkvReviewPrediction(
                    retrievability=0.50,
                    current_interval=11 + candidate.card.id,
                    current_s90=21 + candidate.card.id,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=999,
                        easy=4,
                    ),
                    s90_overrides=RwkvIntervalOverride(
                        again=5,
                        hard=6,
                        good=999,
                        easy=8,
                    ),
                )
                for candidate in candidates
            ]

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            if deck_id == 100:
                return {
                    "id": 1000,
                    "rwkvReviewEnabled": True,
                    "rwkvReviewBatchSize": 64,
                }
            if deck_id == 200:
                return {
                    "id": 2000,
                    "rwkvReviewEnabled": False,
                    "rwkvReviewInstantOrderEnabled": True,
                }
            raise AssertionError(f"unexpected deck {deck_id}")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            assert "id in (1,2,3)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [
                (1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
                (2, 20, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
                (3, 30, 200, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
            ]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(Backend())
    try:
        items = rwkv_scheduler._rwkv_review_reschedule_items(reviewer, [1, 2, 3])
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.preset_id_calls == [[1, 2, 3]]
    assert [
        (item.card_id, item.interval_days, item.elapsed_days, item.s90)
        for item in items
    ] == [
        (1, 12, 39, 22),
        (2, 13, 39, 23),
    ]


def test_rwkv_reschedule_uses_fixed_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = [
        (card_id, _rwkv_review_input(card_id=card_id, note_id=card_id + 1000))
        for card_id in range(1, 130)
    ]
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={8192: inputs},
        loaded_rows=len(inputs),
        parsed_cards=len(inputs),
        cards_with_state=len(inputs),
        disabled_config_cards=0,
        eligible_cards=len(inputs),
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    prediction_batches: list[tuple[int, int]] = []

    def predict(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        *,
        batch_size: int,
    ) -> list[RwkvReviewPrediction]:
        prediction_batches.append((len(inputs_by_card_id), batch_size))
        return [
            RwkvReviewPrediction(
                retrievability=0.5,
                current_interval=10,
                current_s90=20,
            )
            for _ in inputs_by_card_id
        ]

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_predictions_for_inputs",
        predict,
    )

    items = rwkv_scheduler._rwkv_review_reschedule_items_from_input_build(input_build)

    assert len(items) == 129
    assert prediction_batches == [(128, 128), (1, 128)]


def test_rwkv_reschedule_card_ids_use_requested_deck_tree() -> None:
    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "did in (100,101)" in sql
            assert "queue = ?" in sql
            assert args == (2,)
            return [1, 2]

    class Decks:
        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

    mw = SimpleNamespace(col=SimpleNamespace(db=DB(), decks=Decks()))

    assert rwkv_scheduler._rwkv_review_reschedule_card_ids(mw, deck_id=100) == [
        1,
        2,
    ]


def test_rwkv_reschedule_items_use_backend_deck_review_rows() -> None:
    class Backend:
        def __init__(self) -> None:
            self.requests: list[RwkvReviewPredictionRequest] = []

        def cached_review_input_predictions(
            self,
            inputs_by_index: list[tuple[int, RwkvReviewInput]],
        ) -> tuple[
            list[RwkvReviewPrediction | None],
            list[tuple[int, RwkvReviewPredictionRequest]],
            int,
        ]:
            return (
                [None] * len(inputs_by_index),
                [
                    (
                        index,
                        RwkvReviewPredictionRequest(review_input=review_input),
                    )
                    for index, review_input in inputs_by_index
                ],
                0,
            )

        def predict_review_requests(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.requests.extend(requests)
            return [
                RwkvReviewPrediction(
                    retrievability=0.50,
                    current_interval=10 + request.review_input.identity.card_id,
                    current_s90=20 + request.review_input.identity.card_id,
                )
                for request in requests
            ]

    class Rpc:
        def __init__(self) -> None:
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="1000",
                        card_type=2,
                        card_queue=2,
                        card_due=50,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=64,
                    ),
                    SimpleNamespace(
                        card_id=2,
                        note_id=20,
                        deck_id=101,
                        preset_id="1000",
                        card_type=2,
                        card_queue=2,
                        card_due=52,
                        interval_days=6,
                        ease_factor=2400,
                        reps=7,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=36,
                        target_retention=0.86,
                        batch_size=64,
                    ),
                ],
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=3,
            )

    rpc = Rpc()

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": deck_id == 100,
                "rwkvReviewInstantOrderEnabled": deck_id == 101,
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc, decks=Decks()))
    )
    backend = Backend()
    previous_backend = set_reviewer_backend(backend)
    try:
        items = rwkv_scheduler._rwkv_review_reschedule_items_for_deck(
            reviewer,
            100,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False}
    ]
    assert [request.review_input.identity.card_id for request in backend.requests] == [
        1,
    ]
    assert [
        (
            item.card_id,
            item.interval_days,
            item.elapsed_days,
            item.s90,
            item.target_retention,
        )
        for item in items or []
    ] == [
        (1, 11, 39, 21, pytest.approx(0.86)),
    ]


def test_apply_rwkv_review_reschedule_includes_target_retention() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.requests: list[scheduler_pb2.RwkvReviewRescheduleRequest] = []

        def apply_rwkv_review_reschedule_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewRescheduleRequest()
            request.ParseFromString(message)
            self.requests.append(request)
            return b""

    rpc = Rpc()
    mw = SimpleNamespace(col=SimpleNamespace(_backend=rpc))

    rwkv_scheduler._apply_rwkv_review_reschedule(
        mw,
        [
            rwkv_scheduler.RwkvReviewRescheduleItem(
                card_id=1,
                interval_days=12.4,
                elapsed_days=4,
                s90=9.5,
                target_retention=0.50,
            )
        ],
    )

    item = rpc.requests[0].items[0]
    assert item.card_id == 1
    # unrounded (spec sched.rwkv-curve-reschedule)
    assert item.interval == pytest.approx(12.4)
    assert item.target_retention == pytest.approx(0.50)


def test_rwkv_reschedule_items_carry_the_unrounded_interval() -> None:
    """Pins spec/scheduling.md#sched.rwkv-curve-reschedule: the reschedule sends
    RWKV-Curve's unrounded current interval, not the whole days rounded up;
    a backend with whole days only sends those."""

    review_input = _rwkv_review_input(card_id=1, note_id=10)
    items = rwkv_scheduler._rwkv_review_reschedule_items_from_input_predictions(
        [(1, review_input), (2, review_input)],
        [
            RwkvReviewPrediction(
                retrievability=0.8,
                current_interval=11,
                current_interval_unrounded=10.2,
                current_s90=12.5,
            ),
            RwkvReviewPrediction(
                retrievability=0.8, current_interval=11, current_s90=12.5
            ),
        ],
    )

    assert [item.interval_days for item in items] == [
        pytest.approx(10.2),
        pytest.approx(11.0),
    ]


def test_prepare_stats_retrievability_scores_waits_for_pending_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def cache_snapshot(self) -> object:
            return object()

        def restore_cache_snapshot(self, snapshot: object) -> None:
            pass

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            return [RwkvReviewPrediction(retrievability=0.64) for _ in candidates]

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("stats graph scores should use batch prediction")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type = 4" in sql
                return []
            assert "from cards" in sql
            assert "id in (1)" in sql
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, "")]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search == "rated:7"
            assert order is False
            return [1]

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(Backend())
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_pending_generations[key] = 0
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS", 1.0)
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS", 0.001)

    def finish_warmup() -> None:
        time.sleep(0.01)
        rwkv_scheduler._reviewer_backend_warmup_pending_generations.pop(key, None)
        rwkv_scheduler._reviewer_backend_warmup_states[key] = None

    thread = threading.Thread(target=finish_warmup)
    thread.start()
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        thread.join()
        set_reviewer_backend(previous_backend)

    assert len(rpc.stats_calls) == 1
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.64)),
    ]


def test_prepare_stats_retrievability_scores_reports_pending_after_warmup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))
    previous_backend = set_reviewer_backend(object())
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_pending",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_wait_for_reviewer_backend_warmup",
        lambda reviewer, *, timeout_secs: False,
    )

    try:
        status = prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.PENDING
    assert len(rpc.stats_calls) == 1
    assert rpc.stats_calls[0]["search"] == "rated:7"
    assert rpc.stats_calls[0]["scores"] == []


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("ready", rwkv_scheduler.RwkvStatsPreparationStatus.READY),
        ("error", rwkv_scheduler.RwkvStatsPreparationStatus.FAILED),
        ("state_advance", rwkv_scheduler.RwkvStatsPreparationStatus.FAILED),
    ],
)
def test_prepare_stats_retrievability_scores_shares_in_flight_status(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: rwkv_scheduler.RwkvStatsPreparationStatus,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.predict_calls = 0
            self.started = threading.Event()
            self.release = threading.Event()
            self.generation = 7

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            self.predict_calls += 1
            self.started.set()
            assert self.release.wait(timeout=2)
            if outcome == "error":
                raise RuntimeError("prediction failed")
            if outcome == "state_advance":
                self.generation += 1
            return [RwkvReviewPrediction(retrievability=0.64) for _ in candidates]

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("stats graph scores should use batch prediction")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

        def state_generation(self) -> int:
            return self.generation

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            assert "id in (1)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search == "rated:7"
            assert order is False
            return [1]

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    # the memo key holds id()s of the backend and the collection: an earlier
    # parameter's freed objects can hand theirs on, and a kept memo would
    # then answer READY before the backend is asked at all
    rwkv_scheduler.forget_rwkv_stats_scores()
    previous_backend = set_reviewer_backend(backend)
    errors: list[BaseException] = []
    statuses: list[rwkv_scheduler.RwkvStatsPreparationStatus] = []
    waiter_started = threading.Event()
    original_begin = rwkv_scheduler._begin_rwkv_stats_prepare

    def begin(
        key: rwkv_scheduler.RwkvStatsPrepareKey,
    ) -> tuple[Future[rwkv_scheduler.RwkvStatsPreparationStatus], bool]:
        result = original_begin(key)
        if not result[1]:
            waiter_started.set()
        return result

    monkeypatch.setattr(rwkv_scheduler, "_begin_rwkv_stats_prepare", begin)

    def prepare() -> None:
        try:
            statuses.append(prepare_stats_retrievability_scores(reviewer, "rated:7"))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=prepare)
    second = threading.Thread(target=prepare)
    try:
        first.start()
        assert backend.started.wait(timeout=2)
        second.start()
        assert waiter_started.wait(timeout=2)
        backend.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
    finally:
        backend.release.set()
        set_reviewer_backend(previous_backend)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert statuses == [expected_status, expected_status]
    assert backend.predict_calls == 1
    if outcome == "state_advance":
        assert rpc.stats_calls == []
    else:
        assert len(rpc.stats_calls) == 1
        scores = rpc.stats_calls[0]["scores"]
        assert [
            (getattr(score, "card_id"), getattr(score, "retrievability"))
            for score in scores
        ] == ([(1, pytest.approx(0.64))] if outcome == "ready" else [])


def test_concurrent_stats_search_retries_after_backend_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.predict_calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            self.predict_calls += 1
            if self.predict_calls == 1:
                self.started.set()
                assert self.release.wait(timeout=2)
            return [RwkvReviewPrediction(retrievability=0.64) for _ in candidates]

        def state_generation(self) -> int:
            return 7

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            assert "id in (1)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search in {"deck:current", "(deck:current) (-is:suspended)"}
            assert order is False
            return [1]

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    first_statuses: list[rwkv_scheduler.RwkvStatsPreparationStatus] = []

    def prepare_first_search() -> None:
        first_statuses.append(
            prepare_stats_retrievability_scores(reviewer, "deck:current")
        )

    first = threading.Thread(target=prepare_first_search)
    try:
        first.start()
        assert backend.started.wait(timeout=2)

        scoring_contended_status = prepare_stats_retrievability_scores(
            reviewer,
            "(deck:current) (-is:suspended)",
        )
        backend.release.set()
        first.join(timeout=2)

        original_publish = rwkv_scheduler._set_rwkv_stats_graph_scores_if_current
        publish_attempts = 0

        def publish(*args: Any, **kwargs: Any) -> bool:
            nonlocal publish_attempts
            publish_attempts += 1
            if publish_attempts == 1:
                return False
            return original_publish(*args, **kwargs)

        monkeypatch.setattr(
            rwkv_scheduler,
            "_set_rwkv_stats_graph_scores_if_current",
            publish,
        )
        publish_contended_status = prepare_stats_retrievability_scores(
            reviewer,
            "(deck:current) (-is:suspended)",
        )
        retry_status = prepare_stats_retrievability_scores(
            reviewer,
            "(deck:current) (-is:suspended)",
        )
    finally:
        backend.release.set()
        first.join(timeout=2)
        set_reviewer_backend(previous_backend)

    assert not first.is_alive()
    assert first_statuses == [rwkv_scheduler.RwkvStatsPreparationStatus.READY]
    assert scoring_contended_status == rwkv_scheduler.RwkvStatsPreparationStatus.PENDING
    assert publish_contended_status == rwkv_scheduler.RwkvStatsPreparationStatus.PENDING
    assert retry_status == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    assert backend.predict_calls == 3
    assert [call["search"] for call in rpc.stats_calls] == [
        "deck:current",
        "(deck:current) (-is:suspended)",
    ]


def test_prepare_reviewer_queue_order_batches_with_deck_option() -> None:
    class Backend:
        def __init__(self) -> None:
            self.batch_card_ids: list[list[int]] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("single-card prediction should not be used")

        def predict_reviews(self, candidates) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [getattr(getattr(candidate, "card"), "id") for candidate in candidates]
            )
            return [
                RwkvReviewPrediction(retrievability=0.50) for candidate in candidates
            ]

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        batch_size=64,
        card_count=65,
        rwkv_config_in_other=True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.batch_card_ids == [list(range(1, 65)), [65]]
    assert rpc.preset_id_calls == [list(range(1, 66))]
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert len(scores) == 65


def test_prepare_reviewer_queue_order_uses_per_card_scheduling_states() -> None:
    class Backend:
        def __init__(self) -> None:
            self.elapsed_by_card_id: dict[int, int | None] = {}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            identity = rwkv_review_identity(reviewer, card)
            assert identity is not None
            review_input = rwkv_review_input(
                reviewer=reviewer,
                card=card,
                identity=identity,
                ease=None,
            )
            self.elapsed_by_card_id[card.id] = review_input.current_elapsed_days
            return RwkvReviewPrediction(retrievability=0.50)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.elapsed_by_card_id == {1: 4, 2: 1}


def test_prepare_reviewer_queue_order_uses_unknown_elapsed_without_history() -> None:
    class Backend:
        def __init__(self) -> None:
            self.inputs_by_card_id: dict[int, RwkvReviewInput] = {}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            identity = rwkv_review_identity(reviewer, card)
            assert identity is not None
            self.inputs_by_card_id[card.id] = rwkv_review_input(
                reviewer=reviewer,
                card=card,
                identity=identity,
                ease=None,
            )
            return RwkvReviewPrediction(retrievability=0.50)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        latest_review_elapsed_days_by_card={},
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert {
        card_id: review_input.current_elapsed_days
        for card_id, review_input in backend.inputs_by_card_id.items()
    } == {1: None, 2: None}
    assert {
        card_id: review_input.current_state_kind
        for card_id, review_input in backend.inputs_by_card_id.items()
    } == {1: None, 2: None}


def test_prepare_reviewer_queue_order_supports_due_day_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _SharedReviewRuntime()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=0)
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rwkv_scheduler.reviewer_queue_order_enabled(reviewer)
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in cast(list[object], rpc.calls[0]["scores"])
    ] == [(1, pytest.approx(0.45)), (2, pytest.approx(0.45))]


def test_reviewer_rwkv_uses_resolved_fsrs_preset_for_card() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(resolved_preset_id="addon:test:medical")
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)

    assert runtime.query_inputs[0].identity.preset_id == _expected_preset_hash(
        "addon:test:medical"
    )


def test_card_info_queries_rwkv_without_cached_reviewer_prediction() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc, rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    scheduler = reviewer.mw.col.sched
    original_get_scheduling_states = scheduler.get_scheduling_states
    scheduling_state_calls: list[int] = []

    def get_scheduling_states(card_id: int) -> SchedulingStates:
        scheduling_state_calls.append(card_id)
        return original_get_scheduling_states(card_id)

    scheduler.get_scheduling_states = get_scheduling_states

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [("RWKV computed R", "45%")]
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days is None
    assert scheduling_state_calls == [1]
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)}
    ]


def test_card_info_does_not_reinstall_score_after_answer_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc, rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    original = rwkv_scheduler._queried_card_info_diagnostics

    def query_then_answer(
        query_reviewer: object,
        query_card: object,
        *,
        fallback_source: str,
        _candidate: RwkvReviewCandidate | None = None,
    ) -> rwkv_scheduler.RwkvReviewerDiagnostics | None:
        diagnostics = original(
            query_reviewer,
            query_card,
            fallback_source=fallback_source,
            _candidate=_candidate,
        )
        record_reviewer_answer(reviewer, card, ease=3)
        return diagnostics

    monkeypatch.setattr(
        rwkv_scheduler,
        "_queried_card_info_diagnostics",
        query_then_answer,
    )

    rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    )

    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)},
        {"card_id": 1, "retrievability": None},
    ]
    assert 1 not in rpc.active_scores


def test_card_info_reports_rwkv_retrievability_after_review() -> None:
    expected_snapshot = RwkvBackendCacheSnapshot(
        card_states={},
        note_states={},
        deck_states={},
        preset_states={},
        global_state=None,
        runtime_state=b"runtime",
    )

    class Backend:
        def __init__(self) -> None:
            self.answers: list[RwkvReviewInput] = []
            self.queries: list[RwkvReviewInput] = []
            self.call_count = 0

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.45,
                button_probabilities=(0.55, 0.10, 0.20, 0.15),
            )

        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            return expected_snapshot

        def predict_retrievability_after_reviews(
            self,
            *,
            answers: Sequence[RwkvReviewInput],
            inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
            snapshot: RwkvBackendCacheSnapshot,
        ) -> list[list[tuple[int, float]]]:
            self.call_count += 1
            assert snapshot is expected_snapshot
            score_batches: list[list[tuple[int, float]]] = []
            for answer in answers:
                self.answers.append(answer)
                self.queries.extend(
                    review_input for _, review_input in inputs_by_card_id
                )
                assert answer.ease is not None
                score_batches.append(
                    [
                        (
                            card_id,
                            0.6
                            + answer.ease * 0.05
                            + (0.01 if review_input.current_elapsed_seconds else 0),
                        )
                        for card_id, review_input in inputs_by_card_id
                    ]
                )
            return score_batches

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    rwkv_scheduler._reviewer_backend_warmup_states[
        (id(backend), id(reviewer.mw.col))
    ] = None
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card.time_taken = lambda capped=True: (_ for _ in ()).throw(
        AssertionError("Card Info after-review predictions should not read answer time")
    )

    assert rwkv_card_info_after_review_rows(reviewer, card) == [
        (
            "RWKV : R After Review",
            "Again:65% Hard:70% Good:75% Easy:80%",
        ),
        (
            "RWKV : R After 10min",
            "Again:66% Hard:71% Good:76% Easy:81%",
        ),
    ]
    assert backend.call_count == 1
    assert [answer.ease for answer in backend.answers] == [1, 2, 3, 4]
    assert all(query.is_query for query in backend.queries)
    assert all(query.ease is None for query in backend.queries)
    assert [
        (query.current_elapsed_days, query.current_elapsed_seconds)
        for query in backend.queries
    ] == [(0, 0), (0, 600)] * 4


def test_card_info_uses_resident_after_review_prediction_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.future_answers: list[RwkvReviewInput] = []
            self.future_queries: list[RwkvReviewInput] = []

        def predict_retrievability_many_after_reviews_from_warm_up(
            self,
            *,
            answers: Sequence[RwkvReviewInput],
            query_inputs: Sequence[RwkvReviewInput],
        ) -> list[list[float]]:
            self.future_answers.extend(answers)
            self.future_queries.extend(query_inputs)
            return [
                [
                    0.6
                    + cast(int, answer.ease) * 0.05
                    + (0.01 if query.current_elapsed_seconds else 0)
                    for query in query_inputs
                ]
                for answer in answers
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    rwkv_scheduler._reviewer_backend_warmup_states[
        (id(backend), id(reviewer.mw.col))
    ] = None
    monkeypatch.setattr(
        backend,
        "cache_snapshot",
        lambda: pytest.fail("Card Info should not build a full RWKV snapshot"),
    )

    rows = dict(
        rwkv_card_info_after_review_rows(
            reviewer,
            _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        )
    )

    assert rows["RWKV : R After Review"] == ("Again:65% Hard:70% Good:75% Easy:80%")
    assert rows["RWKV : R After 10min"] == ("Again:66% Hard:71% Good:76% Easy:81%")
    assert [answer.ease for answer in runtime.future_answers] == [1, 2, 3, 4]
    assert [
        (query.current_elapsed_days, query.current_elapsed_seconds)
        for query in runtime.future_queries
    ] == [(0, 0), (0, 600)]


def test_future_prediction_snapshot_only_includes_referenced_states() -> None:
    first = _rwkv_review_input(card_id=1, note_id=10)
    second = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        identity=RwkvReviewIdentity(
            card_id=2,
            note_id=20,
            deck_id=200,
            preset_id=2000,
        ),
    )
    snapshot = RwkvBackendCacheSnapshot(
        card_states={1: b"card-1", 2: b"card-2", 3: b"unused-card"},
        note_states={10: b"note-10", 20: b"note-20", 30: b"unused-note"},
        deck_states={100: b"deck-100", 200: b"deck-200", 300: b"unused-deck"},
        preset_states={
            1000: b"preset-1000",
            2000: b"preset-2000",
            3000: b"unused-preset",
        },
        global_state=b"global",
        runtime_state=b"runtime",
    )

    assert _workload_snapshot_for_review_inputs(snapshot, (first, second)) == (
        [(1, b"card-1"), (2, b"card-2")],
        [(10, b"note-10"), (20, b"note-20")],
        [(100, b"deck-100"), (200, b"deck-200")],
        [(1000, b"preset-1000"), (2000, b"preset-2000")],
        b"global",
        b"runtime",
    )


def test_bulk_card_load_qualifies_cards_data_column() -> None:
    queries: list[str] = []

    class DB:
        def all(self, sql: str) -> list[tuple[object, ...]]:
            queries.append(sql)
            return []

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(db=DB())),
    )

    assert (
        rwkv_scheduler._rwkv_card_rows_for_ids(
            reviewer,
            [1],
            reason="test",
        )
        == []
    )
    assert len(queries) == 1
    assert "cards.data" in queries[0]


def test_card_info_refreshes_after_global_rwkv_state_changes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    rpc.active_scores[1] = 0.67
    reviewer = _rwkv_reviewer(
        rpc=rpc, rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    first_rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    )

    backend.review_answered(
        reviewer=reviewer,
        card=_rwkv_card(card_id=2, note_id=20, duration_millis=1234),
        ease=1,
    )
    second_rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    )

    assert dict(first_rows)["RWKV computed R"] == "45%"
    assert dict(second_rows)["RWKV computed R"] == "55%"
    assert runtime.queries == [
        (1, None, None),
        (1, 1, ("deck", 100, 1)),
    ]
    assert rpc.active_score_calls == []
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)},
        {"card_id": 1, "retrievability": pytest.approx(0.55)},
    ]


def test_card_info_uses_shared_card_row_context_for_rwkv_query() -> None:
    class Backend:
        def __init__(self) -> None:
            self.review_inputs: list[RwkvReviewInput] = []

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            candidate = candidates[0]
            identity = rwkv_review_identity(candidate.reviewer, candidate.card)
            assert identity is not None
            self.review_inputs.append(
                rwkv_review_input(
                    reviewer=candidate.reviewer,
                    card=candidate.card,
                    identity=identity,
                    ease=None,
                )
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.61,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=4,
                        easy=8,
                    ),
                )
            ]

    backend = Backend()
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    rwkv_scheduler._reviewer_backend_warmup_states[
        (id(backend), id(reviewer.mw.col))
    ] = None

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=reviewer.cards[2],
        fallback_source="FSRS",
    ) == [("RWKV computed R", "61%")]
    assert backend.review_inputs[0].current_normal_state_kind == "review"
    assert backend.review_inputs[0].current_elapsed_days == 1
    assert backend.review_inputs[0].card_due == 45
    assert rpc.card_info_calls == [
        {"card_id": 2, "retrievability": pytest.approx(0.61)}
    ]


def test_card_info_restores_local_state_cache_before_query(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(
        profile_folder=tmp_path,
        rows=rows,
        deck_config_overrides={
            "rwkvReviewEnabled": False,
            "rwkvReviewInstantOrderEnabled": True,
        },
    )
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    ) == [("RWKV computed R", "45%")]
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    assert restored_runtime.reviewed == []


def test_card_info_skips_rwkv_query_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        historical_review_rows=[],
        rwkv_review_enabled=False,
        rwkv_review_instant_order_enabled=True,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [("RWKV computed R", "Calculating…")]
    assert runtime.queries == []


def test_card_info_configures_embedded_backend_for_rwkv_enabled_card(
    monkeypatch, tmp_path
) -> None:
    created: list[dict[str, object]] = []
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.66,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=2,
                    good=4,
                    easy=8,
                ),
            )

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert rwkv_card_info_rows(
        reviewer=_rwkv_reviewer(
            rwkv_review_enabled=False, rwkv_review_instant_order_enabled=True
        ),
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    ) == [("RWKV computed R", "66%")]
    assert created == [
        {
            "model_path": model_path,
            "device": "cpu",
            "dtype": "float",
        }
    ]


def _card_runs_rwkv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare reviewers below have no presets: their card counts as an RWKV
    card with a ready state (an FSRS-7 card gets no prediction, spec
    ui.fsrs7-no-rwkv-values)."""
    for name in (
        "rwkv_review_active",
        "_reviewer_backend_ready_for_review",
        "_reviewer_backend_warmed_up",
    ):
        monkeypatch.setattr(rwkv_scheduler, name, lambda *args: True)


def test_reviewer_rwkv_prediction_is_a_query_until_review_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _card_runs_rwkv(monkeypatch)
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = SimpleNamespace()
    card = SimpleNamespace(id=1)

    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)

    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.45)
    assert runtime.reviewed == []


def test_srs_benchmark_backend_builds_query_and_answer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def item(self) -> float:
            return 0.72

    class Process:
        def __init__(self) -> None:
            self.query_rows: list[dict[str, object]] = []
            self.answer_rows: list[dict[str, object]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            self.query_rows.append(row)
            return Probability()

        def process_row(self, row: dict[str, object]) -> None:
            self.answer_rows.append(row)

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    reviewer = _rwkv_reviewer()
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: now)
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 7 * 86_400,
    )

    prediction = backend.predict_review(reviewer=reviewer, card=card)
    backend.review_answered(reviewer=reviewer, card=card, ease=3)

    assert prediction is not None
    assert prediction.retrievability == pytest.approx(0.72)
    assert process.query_rows == [
        {
            "card_id": 1,
            "note_id": 10,
            "deck_id": 100,
            "preset_id": 1000,
            "elapsed_days": 7,
            "elapsed_seconds": 604800,
            "day_offset": 42,
            "duration": 0.0,
            "state": 2,
            "rating": 1,
        }
    ]
    assert process.answer_rows[0]["duration"] == pytest.approx(1234.0)
    assert process.answer_rows[0]["rating"] == 3


def test_srs_benchmark_backend_warmup_processes_historical_rows() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def item(self) -> float:
            return 0.72

    class Process:
        def __init__(self) -> None:
            self.query_rows: list[dict[str, object]] = []
            self.answer_rows: list[dict[str, object]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            self.query_rows.append(row)
            return Probability()

        def process_row(self, row: dict[str, object]) -> object:
            self.answer_rows.append(row)
            return object()

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    recorded: list[tuple[int, float]] = []
    progress: list[RwkvWarmUpProgress] = []
    backend.warm_up(
        [
            RwkvReviewInput(
                identity=RwkvReviewIdentity(
                    card_id=1,
                    note_id=10,
                    deck_id=100,
                    preset_id=1000,
                ),
                is_query=False,
                ease=3,
                duration_millis=1234,
                card_type=2,
                card_queue=2,
                card_due=None,
                interval_days=4,
                ease_factor=2500,
                reps=None,
                lapses=None,
                day_offset=42,
                current_state_kind="normal",
                current_normal_state_kind="review",
                current_elapsed_days=7,
                current_elapsed_seconds=604800,
            )
        ],
        review_ids=[123],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
        progress=progress.append,
    )

    assert recorded == [(123, pytest.approx(0.72))]
    assert progress == [
        RwkvWarmUpProgress(processed_reviews=0, total_reviews=1),
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=1),
    ]
    assert process.query_rows == [
        {
            "card_id": 1,
            "note_id": 10,
            "deck_id": 100,
            "preset_id": 1000,
            "elapsed_days": 7,
            "elapsed_seconds": 604800,
            "day_offset": 42,
            "duration": 0.0,
            "state": 2,
            "rating": 1,
        }
    ]
    assert len(process.answer_rows) == 1
    assert process.answer_rows[0]["card_id"] == 1
    assert process.answer_rows[0]["rating"] == 3
    assert process.answer_rows[0]["duration"] == pytest.approx(1234.0)


def test_srs_benchmark_backend_updates_other_card_retrievability() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def __init__(self) -> None:
            self.review_count = 0

        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.40 + 0.20 * self.review_count)

        def process_row(self, row: dict[str, object]) -> None:
            self.review_count += 1

    backend = SrsBenchmarkRwkvReviewerBackend(process=Process())
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    before = backend.predict_review(reviewer=reviewer, card=card_b)
    backend.review_answered(reviewer=reviewer, card=card_a, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=card_b)

    assert before is not None
    assert after is not None
    assert before.retrievability == pytest.approx(0.40)
    assert after.retrievability == pytest.approx(0.60)


def test_srs_benchmark_backend_predict_review_retrievability_skips_curve() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.75)

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            raise AssertionError("RWKV-Curve should not be used for live grades")

    backend = SrsBenchmarkRwkvReviewerBackend(process=Process())
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    prediction = backend.predict_review_retrievability(reviewer=reviewer, card=card)

    assert prediction is not None
    assert prediction.retrievability == pytest.approx(0.75)
    assert prediction.current_interval is None
    assert prediction.current_s90 is None
    assert prediction.interval_overrides == RwkvIntervalOverride()
    assert prediction.s90_overrides == RwkvIntervalOverride()


def test_srs_benchmark_backend_uses_ahead_curve_for_interval_overrides() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.80)

        def process_row(self, row: dict[str, object]) -> object:
            return object()

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            elapsed_days = elapsed_seconds // 86_400
            return Probability(1.0 - elapsed_days * 0.025)

    backend = SrsBenchmarkRwkvReviewerBackend(
        process=Process(),
        target_retention=0.90,
        max_interval_days=30,
    )
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    before = backend.predict_review(reviewer=reviewer, card=card)
    backend.review_answered(reviewer=reviewer, card=card, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=card)

    assert before is not None
    assert before.current_interval is None
    assert before.current_s90 is None
    assert before.interval_overrides == RwkvIntervalOverride()
    assert before.s90_overrides == RwkvIntervalOverride()
    assert after is not None
    assert after.retrievability == pytest.approx(0.80)
    assert after.current_interval == 4
    assert after.current_s90 == 4
    assert after.interval_overrides == RwkvIntervalOverride(
        again=4,
        hard=4,
        good=4,
        easy=4,
    )
    assert after.s90_overrides == RwkvIntervalOverride(
        again=4,
        hard=4,
        good=4,
        easy=4,
    )


def test_srs_benchmark_backend_batches_predictions() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def __init__(self) -> None:
            self.rows: list[list[dict[str, object]]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            raise AssertionError("single-card prediction should not be used")

        def imm_predict_many(self, rows: list[dict[str, object]]) -> list[Probability]:
            self.rows.append(rows)
            return [Probability(0.10 * int(row["card_id"])) for row in rows]

        def process_row(self, row: dict[str, object]) -> object:
            return object()

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            return Probability(0.80)

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=2345)

    predictions = backend.predict_reviews(
        [
            SimpleNamespace(reviewer=reviewer, card=card_a),
            SimpleNamespace(reviewer=reviewer, card=card_b),
        ]
    )

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.10),
        pytest.approx(0.20),
    ]
    assert [[row["card_id"] for row in rows] for rows in process.rows] == [[1, 2]]


def test_embedded_rust_runtime_batches_bridge_predictions() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[
            tuple[
                float,
                float | None,
                int | None,
                int | None,
                tuple[int | None, ...],
                tuple[int | None, ...],
                tuple[float, float, float, float],
                float | None,
            ]
        ]:
            self.requests.append(requests)
            return [
                (
                    0.25,
                    0.65,
                    9,
                    19,
                    (1, 3, 7, 14),
                    (2, 4, 17, 28),
                    (0.75, 0.05, 0.15, 0.05),
                    8.4,
                ),
                (
                    0.75,
                    None,
                    None,
                    None,
                    (None, None, None, None),
                    (None, None, None, None),
                    (0.25, 0.10, 0.50, 0.15),
                    None,
                ),
            ]

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    requests = [
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=1, note_id=10),
            card_state=b"card-1",
            note_state=b"note-10",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=2, note_id=20),
            card_state=b"card-2",
            note_state=b"note-20",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
    ]

    predictions = runtime.predict_many(requests)

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.25),
        pytest.approx(0.75),
    ]
    first, second = predictions
    assert first is not None
    assert first.curve_retrievability == pytest.approx(0.65)
    assert first.current_interval == 9
    assert first.current_interval_unrounded == pytest.approx(8.4)
    assert first.current_s90 == 19
    assert first.interval_overrides == RwkvIntervalOverride(
        again=1,
        hard=3,
        good=7,
        easy=14,
    )
    assert first.s90_overrides == RwkvIntervalOverride(
        again=2,
        hard=4,
        good=17,
        easy=28,
    )
    assert first.button_probabilities == pytest.approx((0.75, 0.05, 0.15, 0.05))
    assert second is not None
    assert second.curve_retrievability is None
    assert second.current_interval is None
    assert second.current_interval_unrounded is None
    assert second.current_s90 is None
    assert second.interval_overrides == RwkvIntervalOverride()
    assert second.s90_overrides == RwkvIntervalOverride()
    assert second.button_probabilities == pytest.approx((0.25, 0.10, 0.50, 0.15))
    assert process.requests == [
        [
            (
                1,
                10,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                None,
                None,
                None,
                None,
                True,
                b"card-1",
                b"note-10",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
            (
                2,
                20,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                None,
                None,
                None,
                None,
                True,
                b"card-2",
                b"note-20",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
        ]
    ]


def test_embedded_rust_runtime_batches_retrievability_bridge_predictions() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            self.requests.append(requests)
            return [0.25, 0.75]

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    requests = [
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=1, note_id=10),
            card_state=b"card-1",
            note_state=b"note-10",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=2, note_id=20),
            card_state=b"card-2",
            note_state=b"note-20",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
    ]

    retrievabilities = runtime.predict_retrievability_many(requests)

    assert retrievabilities == [pytest.approx(0.25), pytest.approx(0.75)]
    assert process.requests == [
        [
            (
                1,
                10,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                None,
                None,
                None,
                None,
                True,
                b"card-1",
                b"note-10",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
            (
                2,
                20,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                None,
                None,
                None,
                None,
                True,
                b"card-2",
                b"note-20",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
        ]
    ]


def test_embedded_rust_runtime_prefers_tuple_retrievability_bridge() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_retrievability_many_packed(
            self,
            requests: bytes,
            state_columns: tuple[
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
            ],
        ) -> list[float]:
            raise AssertionError("packed path should not be used")

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            self.requests.append(requests)
            return [0.25, 0.75]

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    requests = [
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=1, note_id=10),
            card_state=b"card-1",
            note_state=b"note-10",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=2, note_id=20),
            card_state=b"card-2",
            note_state=b"note-20",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
    ]

    retrievabilities = runtime.predict_retrievability_many(requests)

    assert retrievabilities == [pytest.approx(0.25), pytest.approx(0.75)]
    assert len(process.requests) == 1
    assert [request[0] for request in process.requests[0]] == [1, 2]


def _warm_up_review_input(*, card_id: int, note_id: int, ease: int) -> RwkvReviewInput:
    return replace(
        _rwkv_review_input(card_id=card_id, note_id=note_id),
        is_query=False,
        ease=ease,
        duration_millis=1234,
    )


def test_embedded_rust_runtime_prefers_packed_warm_up_reviews() -> None:
    import struct

    from aqt.rwkv_srs_benchmark import (
        _PACKED_PREDICTION_REQUEST_HEADER,
        _PACKED_PREDICTION_REQUEST_ROW,
        _PACKED_WARM_UP_REVIEW_MAGIC,
        _RustRwkvRuntime,
    )

    class Process:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []
            self.record_flags: list[bool] = []

        def warm_up_reviews_packed(
            self,
            reviews: bytes,
            record_predictions: bool,
        ) -> list[tuple[int, float]]:
            self.payloads.append(reviews)
            self.record_flags.append(record_predictions)
            return [(0, 0.31 if len(self.payloads) == 1 else 0.42)]

        def warm_up_reviews(self, *args: object) -> list[tuple[int, float]]:
            raise AssertionError("packed warm-up path should be used")

        def warm_up_snapshot(
            self,
        ) -> tuple[object, object, object, object, bytes | None, bytes]:
            return (
                [(1, b"card-1"), (2, b"card-2")],
                [(10, b"note-10")],
                [(100, b"deck-100")],
                [(1000, b"preset-1000")],
                b"global",
                b"runtime",
            )

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    recorded: list[tuple[int, float]] = []
    checkpoint_snapshots: list[tuple[int, RwkvBackendCacheSnapshot]] = []

    snapshot = runtime.warm_up_reviews(
        [
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
        ],
        review_ids=[101, 102],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
        snapshot_after_reviews=[1],
        snapshot_recorder=lambda review_count, state: checkpoint_snapshots.append(
            (review_count, state)
        ),
    )

    # Small histories chunk at the progress interval, so each review arrives
    # in its own packed call.
    assert process.record_flags == [True, True]
    assert recorded == [(101, pytest.approx(0.31)), (102, pytest.approx(0.42))]
    assert snapshot.card_states == {1: b"card-1", 2: b"card-2"}
    assert snapshot.runtime_state == b"runtime"
    assert checkpoint_snapshots == [(1, snapshot)]

    # note/deck/preset/ease/duration/card_type/day_offset/elapsed presence
    # bits 0-8 set, target retention bits 9-12 clear.
    presence = 0b1_1111_1111
    header = _PACKED_PREDICTION_REQUEST_HEADER.pack(_PACKED_WARM_UP_REVIEW_MAGIC, 1)
    assert process.payloads == [
        header
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            1,
            10,
            100,
            1000,
            0,
            2,
            1234,
            2,
            42,
            7,
            604800,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        ),
        header
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            2,
            20,
            100,
            1000,
            0,
            3,
            1234,
            2,
            42,
            7,
            604800,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        ),
    ]
    header_size = struct.calcsize("<8sI")
    assert len(process.payloads[0]) == header_size + _PACKED_PREDICTION_REQUEST_ROW.size


def test_embedded_rust_runtime_packs_memorised_day_queries() -> None:
    import struct

    from aqt.rwkv_srs_benchmark import (
        _PACKED_PREDICTION_REQUEST_HEADER,
        _PACKED_PREDICTION_REQUEST_ROW,
        _PACKED_WARM_UP_REVIEW_MAGIC,
        _RustRwkvRuntime,
    )

    class Process:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []

        def predict_retrievability_many_from_warm_up_packed(
            self,
            inputs: bytes,
        ) -> bytes:
            self.payloads.append(inputs)
            return struct.pack("<2f", 0.25, 0.75)

        def predict_retrievability_many_from_warm_up(
            self,
            _inputs: object,
        ) -> list[float]:
            raise AssertionError("tuple resident path should not be used")

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    reviews = [
        replace(
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            day_offset=40,
        ),
        replace(
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
            day_offset=41,
        ),
    ]

    raw = runtime.predict_memorised_retrievability_from_warm_up(reviews, day=42)

    assert struct.unpack("<2f", raw) == pytest.approx((0.25, 0.75))
    # Query rows keep identity and state-shaping fields, while answer-only
    # ease/duration are absent. Bits: note/deck/preset/card_type/day/elapsed.
    presence = 0b1_1110_0111
    header = _PACKED_PREDICTION_REQUEST_HEADER.pack(
        _PACKED_WARM_UP_REVIEW_MAGIC,
        2,
    )
    assert process.payloads == [
        header
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            1,
            10,
            100,
            1000,
            1,
            0,
            0,
            2,
            42,
            2,
            172_800,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        )
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            2,
            20,
            100,
            1000,
            1,
            0,
            0,
            2,
            42,
            1,
            86_400,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        )
    ]


def test_embedded_rust_runtime_warm_up_falls_back_to_tuple_rows() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.calls: list[tuple[list[tuple[object, ...]], bool]] = []

        def warm_up_reviews(
            self,
            rows: list[tuple[object, ...]],
            record_predictions: bool,
        ) -> list[tuple[int, float]]:
            self.calls.append((list(rows), record_predictions))
            return []

        def warm_up_snapshot(
            self,
        ) -> tuple[object, object, object, object, bytes | None, bytes]:
            return ([], [], [], [], None, b"runtime")

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process

    runtime.warm_up_reviews(
        [
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
        ]
    )

    # State-only replay keeps a small history together so the native
    # wavefront can schedule all available review chunks.
    assert len(process.calls) == 1
    assert all(record_predictions is False for _, record_predictions in process.calls)
    rows = [row for chunk_rows, _ in process.calls for row in chunk_rows]
    assert [row[0] for row in rows] == [1, 2]
    assert [row[5] for row in rows] == [2, 3]


def test_embedded_rust_runtime_serializes_retrievability_bridge_calls() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.active = False
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls: list[list[int]] = []
            self.overlapped = False

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            if self.active:
                self.overlapped = True
                raise RuntimeError("Already borrowed")

            self.active = True
            self.calls.append([int(request[0]) for request in requests])
            try:
                if len(self.calls) == 1:
                    self.entered.set()
                    assert self.release.wait(2)
                return [0.25 for _ in requests]
            finally:
                self.active = False

    def request(card_id: int) -> RwkvReviewPredictionRequest:
        return RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=card_id, note_id=card_id * 10),
        )

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    errors: list[Exception] = []
    outputs: list[list[float]] = []

    def predict(card_id: int) -> None:
        try:
            outputs.append(
                list(runtime.predict_retrievability_many([request(card_id)]))
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=lambda: predict(1))
    first.start()
    assert process.entered.wait(2)

    second = threading.Thread(target=lambda: predict(2))
    second.start()
    time.sleep(0.05)
    assert second.is_alive()

    process.release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert outputs == [[pytest.approx(0.25)], [pytest.approx(0.25)]]
    assert process.calls == [[1], [2]]
    assert not process.overlapped


def test_configure_reviewer_backend_uses_srs_benchmark_override(monkeypatch) -> None:
    created: list[dict[str, object]] = []

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.61)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.setenv("ANKI_RWKV_BENCHMARK_PATH", "/tmp/srs-benchmark")
    monkeypatch.setenv("ANKI_RWKV_MODEL_PATH", "/tmp/rwkv.pth")
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "cpu")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "float")
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.SrsBenchmarkRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "benchmark_path": "/tmp/srs-benchmark",
            "model_path": "/tmp/rwkv.pth",
            "device": "cpu",
            "dtype": "float",
        }
    ]
    reviewer = SimpleNamespace()
    card = SimpleNamespace(id=1)
    _card_runs_rwkv(monkeypatch)
    update_reviewer_scheduling_states(
        SchedulingStates(),
        reviewer,
        card,
    )
    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.61)


def test_configure_reviewer_backend_uses_embedded_default(
    monkeypatch, tmp_path
) -> None:
    created: list[dict[str, object]] = []
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.73)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "cpu")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "float")
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "model_path": model_path,
            "device": "cpu",
            "dtype": "float",
        }
    ]


def test_configure_reviewer_backend_treats_missing_torch_as_unavailable(
    monkeypatch, tmp_path, caplog
) -> None:
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            raise ModuleNotFoundError("No module named 'torch'", name="torch")

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    with caplog.at_level("ERROR", logger="aqt.rwkv_scheduler"):
        assert configure_reviewer_backend_from_environment() is False

    assert "failed to configure RWKV scheduler backend" not in caplog.text


def test_configure_reviewer_backend_uses_model_env_with_embedded_runner(
    monkeypatch,
) -> None:
    created: list[dict[str, object]] = []

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.73)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setenv("ANKI_RWKV_MODEL_PATH", "/tmp/custom-rwkv.pth")
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "mps")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "bfloat16")
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "model_path": Path("/tmp/custom-rwkv.pth"),
            "device": "mps",
            "dtype": "bfloat16",
        }
    ]


class _SharedReviewRuntime:
    def __init__(self) -> None:
        self.reviewed: list[tuple[int, int]] = []
        self.queries: list[tuple[int, object | None, object | None]] = []
        self.query_inputs: list[RwkvReviewInput] = []
        self.answered_inputs: list[RwkvReviewInput] = []
        self.runtime_review_count = 0

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        identity = review_input.identity
        review_count = global_state if isinstance(global_state, int) else 0
        ease = review_input.ease
        if ease is None:
            self.query_inputs.append(review_input)
            self.queries.append((identity.card_id, global_state, deck_state))
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(
                    retrievability=0.45 + review_count * 0.10,
                    interval_overrides=RwkvIntervalOverride(
                        again=2 + review_count,
                        hard=3 + review_count,
                        good=5 + review_count,
                        easy=8 + review_count,
                    ),
                    s90_overrides=RwkvIntervalOverride(
                        again=3 + review_count,
                        hard=4 + review_count,
                        good=6 + review_count,
                        easy=9 + review_count,
                    ),
                ),
            )

        self.answered_inputs.append(review_input)
        self.reviewed.append((identity.card_id, ease))
        self.runtime_review_count += 1
        return RwkvReviewTransition(
            card_state=("card", identity.card_id, ease),
            note_state=("note", identity.note_id, ease),
            deck_state=("deck", identity.deck_id, review_count + 1),
            preset_state=("preset", identity.preset_id, review_count + 1),
            global_state=review_count + 1,
        )

    def snapshot(self, review_input: RwkvReviewInput) -> object:
        return self.runtime_review_count

    def restore(self, state: object | None) -> None:
        self.runtime_review_count = state if isinstance(state, int) else 0


class _CacheRuntime:
    def __init__(self) -> None:
        self.reviewed: list[tuple[int, int]] = []
        self.answered_inputs: list[RwkvReviewInput] = []
        self.restored_cache_states: list[bytes] = []

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        del card_state, note_state, deck_state, preset_state, global_state
        identity = review_input.identity
        ease = review_input.ease
        if ease is None:
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(retrievability=0.45)
            )

        self.reviewed.append((identity.card_id, ease))
        self.answered_inputs.append(review_input)
        return RwkvReviewTransition(
            card_state=f"card-{identity.card_id}-{ease}".encode(),
            note_state=f"note-{identity.note_id}-{ease}".encode(),
            deck_state=f"deck-{identity.deck_id}-{ease}".encode(),
            preset_state=f"preset-{identity.preset_id}-{ease}".encode(),
            global_state=f"global-{len(self.reviewed)}".encode(),
        )

    def cache_state(self) -> bytes:
        return b"runtime-cache"

    def restore_cache_state(self, state: bytes) -> None:
        self.restored_cache_states.append(state)


class _ResidentCacheRuntime:
    resident_warm_up_state = True

    def __init__(self) -> None:
        self.card_states: dict[int, bytes] = {}
        self.note_states: dict[int, bytes] = {}
        self.deck_states: dict[int, bytes] = {}
        self.preset_states: dict[int, bytes] = {}
        self.global_state: bytes | None = None
        self.return_snapshot_flags: list[bool] = []
        self.reset_count = 0

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        del card_state, note_state, deck_state, preset_state, global_state
        identity = review_input.identity
        ease = review_input.ease
        if ease is None:
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(retrievability=0.45)
            )

        card = f"card-{identity.card_id}-{ease}".encode()
        note = f"note-{identity.note_id}-{ease}".encode()
        deck = f"deck-{identity.deck_id}-{ease}".encode()
        preset = f"preset-{identity.preset_id}-{ease}".encode()
        global_value = f"global-{len(self.card_states) + 1}".encode()
        self.card_states[identity.card_id] = card
        if identity.note_id is not None:
            self.note_states[identity.note_id] = note
        if identity.deck_id is not None:
            self.deck_states[identity.deck_id] = deck
        if identity.preset_id is not None:
            self.preset_states[identity.preset_id] = preset
        self.global_state = global_value
        return RwkvReviewTransition(
            card_state=card,
            note_state=note,
            deck_state=deck,
            preset_state=preset,
            global_state=global_value,
        )

    def warm_up_reviews(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: object | None = None,
        progress: object | None = None,
        snapshot_after_reviews: Sequence[int] = (),
        snapshot_recorder: object | None = None,
        return_snapshot: bool = True,
    ) -> RwkvBackendCacheSnapshot | None:
        del review_ids, prediction_recorder, progress
        endpoints = set(snapshot_after_reviews)
        for processed, review_input in enumerate(reviews, start=1):
            state = self.warm_up_state(review_input)
            self.review(
                review_input=review_input,
                card_state=state.card_state,
                note_state=state.note_state,
                deck_state=state.deck_state,
                preset_state=state.preset_state,
                global_state=state.global_state,
            )
            if processed in endpoints and callable(snapshot_recorder):
                snapshot_recorder(processed, self.warm_up_snapshot())
        self.return_snapshot_flags.append(return_snapshot)
        return self.warm_up_snapshot() if return_snapshot else None

    def warm_up_state(
        self,
        review_input: RwkvReviewInput,
    ) -> RwkvReviewerStateSnapshot:
        identity = review_input.identity
        return RwkvReviewerStateSnapshot(
            card_state=self.card_states.get(identity.card_id),
            note_state=(
                self.note_states.get(identity.note_id)
                if identity.note_id is not None
                else None
            ),
            deck_state=(
                self.deck_states.get(identity.deck_id)
                if identity.deck_id is not None
                else None
            ),
            preset_state=(
                self.preset_states.get(identity.preset_id)
                if identity.preset_id is not None
                else None
            ),
            global_state=self.global_state,
        )

    def warm_up_snapshot(self) -> RwkvBackendCacheSnapshot:
        return RwkvBackendCacheSnapshot(
            card_states=dict(self.card_states),
            note_states=dict(self.note_states),
            deck_states=dict(self.deck_states),
            preset_states=dict(self.preset_states),
            global_state=self.global_state,
            runtime_state=b"runtime",
        )

    def restore_warm_up_snapshot(self, snapshot: RwkvBackendCacheSnapshot) -> None:
        self.card_states = dict(snapshot.card_states)
        self.note_states = dict(snapshot.note_states)
        self.deck_states = dict(snapshot.deck_states)
        self.preset_states = dict(snapshot.preset_states)
        self.global_state = snapshot.global_state

    def restore_warm_up_state(
        self,
        identity: RwkvReviewIdentity,
        snapshot: RwkvReviewerStateSnapshot,
    ) -> None:
        _set_or_remove_test_state(
            self.card_states,
            identity.card_id,
            snapshot.card_state,
        )
        _set_or_remove_test_state(
            self.note_states, identity.note_id, snapshot.note_state
        )
        _set_or_remove_test_state(
            self.deck_states, identity.deck_id, snapshot.deck_state
        )
        _set_or_remove_test_state(
            self.preset_states,
            identity.preset_id,
            snapshot.preset_state,
        )
        self.global_state = cast(bytes | None, snapshot.global_state)

    def reset_warm_up_state(self) -> None:
        self.reset_count += 1
        self.card_states.clear()
        self.note_states.clear()
        self.deck_states.clear()
        self.preset_states.clear()
        self.global_state = None

    def cache_state(self) -> bytes:
        return b"runtime"

    def restore_cache_state(self, state: bytes) -> None:
        assert state == b"runtime"


def _set_or_remove_test_state(
    states: dict[int, bytes],
    identity: int | None,
    state: object | None,
) -> None:
    if identity is None:
        return
    if isinstance(state, bytes):
        states[identity] = state
    else:
        states.pop(identity, None)


class _UndoCounter:
    def __init__(self, reviewer: SimpleNamespace) -> None:
        self.value = 0
        reviewer.mw.col.undo_status = self.undo_status

    def set(self, value: int) -> None:
        self.value = value

    def undo_status(self) -> SimpleNamespace:
        return SimpleNamespace(last_step=self.value)


def _undo_result(*, counter: int, next_counter: int) -> SimpleNamespace:
    return SimpleNamespace(
        counter=counter,
        new_status=SimpleNamespace(last_step=next_counter),
    )


class _RwkvQueueScoreRpc:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.patch_calls: list[
            scheduler_pb2.RwkvAnsweredCardQueueScorePatchRequest
        ] = []
        self.intervening_calls: list[
            scheduler_pb2.RwkvReviewQueueInterveningReviewsRequest
        ] = []
        self.stats_calls: list[dict[str, object]] = []
        self.card_info_calls: list[dict[str, object]] = []
        self.preset_id_calls: list[list[int]] = []
        self.active_scores: dict[int, float] = {}
        self.active_score_calls: list[int] = []

    def set_rwkv_review_queue_scores(
        self,
        *,
        deck_id: int,
        scores: list[object],
    ) -> None:
        self.calls.append({"deck_id": deck_id, "scores": scores})
        if not scores:
            self.active_scores.clear()
        else:
            self.active_scores = {
                getattr(score, "card_id"): getattr(score, "retrievability")
                for score in scores
            }

    def patch_answered_card_rwkv_review_queue_score_raw(
        self,
        message: bytes,
    ) -> bytes:
        request = scheduler_pb2.RwkvAnsweredCardQueueScorePatchRequest()
        request.ParseFromString(message)
        self.patch_calls.append(request)
        if request.HasField("score"):
            self.active_scores[request.card_id] = request.score.retrievability
        else:
            self.active_scores.pop(request.card_id, None)
        return b""

    def update_rwkv_review_queue_intervening_reviews_raw(
        self,
        message: bytes,
    ) -> bytes:
        request = scheduler_pb2.RwkvReviewQueueInterveningReviewsRequest()
        request.ParseFromString(message)
        self.intervening_calls.append(request)
        return b""

    def set_rwkv_stats_graph_scores(
        self,
        *,
        search: str,
        scores: list[object],
    ) -> None:
        self.stats_calls.append({"search": search, "scores": scores})

    def set_rwkv_card_info_score(self, message: Any) -> None:
        card_id = getattr(message, "card_id")
        retrievability = (
            getattr(message, "retrievability")
            if message.HasField("retrievability")
            else None
        )
        self.card_info_calls.append(
            {
                "card_id": card_id,
                "retrievability": retrievability,
            }
        )
        if retrievability is None:
            self.active_scores.pop(card_id, None)
        else:
            self.active_scores[card_id] = retrievability

    def _active_score_response(
        self,
        card_id: int,
    ) -> scheduler_pb2.RwkvRetrievabilityScoreResponse:
        self.active_score_calls.append(card_id)
        score = self.active_scores.get(card_id)
        response = scheduler_pb2.RwkvRetrievabilityScoreResponse()
        if score is not None:
            response.retrievability = score
        return response

    def get_rwkv_retrievability_score_raw(self, message: bytes) -> bytes:
        request = cards_pb2.CardId()
        request.ParseFromString(message)
        return self._active_score_response(request.cid).SerializeToString()

    def get_rwkv_retrievability_score(self, card_id: int) -> object:
        return self._active_score_response(card_id)

    def get_fsrs_preset_ids_for_cards(self, cids: list[int]) -> SimpleNamespace:
        self.preset_id_calls.append(list(cids))
        return SimpleNamespace(
            items=[
                SimpleNamespace(card_id=card_id, preset_id="1000") for card_id in cids
            ]
        )


def _rwkv_queue_reviewer(
    *,
    rpc: _RwkvQueueScoreRpc,
    review_order: int,
    new_gather_priority: int | None = None,
    batch_size: int | None = None,
    card_count: int = 2,
    rwkv_config_in_other: bool = False,
    rwkv_curve_enabled: bool = False,
    rwkv_instant_order_enabled: bool = True,
    rwkv_candidate_refresh_enabled: bool = False,
    rwkv_min_intervening_reviews: int = 0,
    latest_review_elapsed_days_by_card: dict[int, int] | None = None,
) -> SimpleNamespace:
    cards = {
        card_id: _rwkv_card(
            card_id=card_id,
            note_id=card_id * 10,
            duration_millis=1234,
        )
        for card_id in range(1, card_count + 1)
    }
    if 1 in cards:
        cards[1].due = 42
    if 2 in cards:
        cards[2].due = 45

    if latest_review_elapsed_days_by_card is None:
        latest_review_elapsed_days_by_card = {1: 4, 2: 1}

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "did in (100,101)" in sql
            assert "queue = ?" in sql
            assert args == (2,)
            return list(cards)

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            id_prefix = "cid in (" if "from revlog" in sql else "id in ("
            requested_ids_start = sql.index(id_prefix) + len(id_prefix)
            requested_ids_end = sql.index(")", requested_ids_start)
            requested_ids = {
                int(card_id)
                for card_id in sql[requested_ids_start:requested_ids_end].split(",")
            }
            assert requested_ids <= set(cards)
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type in (0, 1, 2, 3, 4, 5)" in sql
                assert "not (type = 3 and factor = 0)" in sql
                return [
                    (
                        card_id,
                        (43 - elapsed_days) * 86_400 * 1000,
                    )
                    for card_id, elapsed_days in latest_review_elapsed_days_by_card.items()
                    if card_id in requested_ids
                ]

            assert "from cards" in sql
            return [
                (
                    card.id,
                    card.nid,
                    card.did,
                    0,
                    card.type,
                    card.queue,
                    card.due,
                    0,
                    card.ivl,
                    card.factor,
                    card.reps,
                    card.lapses,
                    "",
                )
                for card in cards.values()
                if card.id in requested_ids
            ]

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            config: dict[str, object] = {
                "id": deck_id * 10,
                "reviewOrder": review_order,
            }
            if new_gather_priority is not None:
                config["newCardGatherPriority"] = new_gather_priority
            if rwkv_config_in_other:
                nested: dict[str, object] = {"rwkv_review_enabled": rwkv_curve_enabled}
                nested["rwkv_review_instant_order_enabled"] = rwkv_instant_order_enabled
                nested["rwkv_review_min_intervening_reviews"] = (
                    rwkv_min_intervening_reviews
                )
                nested["rwkv_review_min_elapsed_secs"] = 0
                if rwkv_candidate_refresh_enabled:
                    nested["rwkv_review_candidate_refresh_enabled"] = True
                if batch_size is not None:
                    nested["rwkv_review_batch_size"] = batch_size
                config["other"] = {"jschoreels.rwkv": nested}
            else:
                config["rwkvReviewEnabled"] = rwkv_curve_enabled
                config["rwkvReviewInstantOrderEnabled"] = rwkv_instant_order_enabled
                config["rwkvReviewMinInterveningReviews"] = rwkv_min_intervening_reviews
                config["rwkvReviewMinElapsedSecs"] = 0
                if rwkv_candidate_refresh_enabled:
                    config["rwkvReviewCandidateRefreshEnabled"] = True
                if batch_size is not None:
                    config["rwkvReviewBatchSize"] = batch_size
            return config

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            raise AssertionError("queue scores should bulk-load card rows")

    col = SimpleNamespace(
        _backend=rpc,
        db=DB(),
        decks=Decks(),
        sched=Scheduler(),
        get_card=lambda card_id: (_ for _ in ()).throw(
            AssertionError("queue scores should bulk-load card rows")
        ),
    )
    return SimpleNamespace(mw=SimpleNamespace(col=col), cards=cards)


def _rwkv_reviewer(
    *,
    rwkv_review_enabled: bool = True,
    rwkv_review_enforce_grade_order: bool = True,
    rwkv_review_instant_order_enabled: bool = False,
    rwkv_review_dynamic_preset_replay: bool = False,
    rwkv_review_first_review_elapsed_from_card_creation: bool = False,
    resolved_preset_id: str | None = "1000",
    preset_desired_retention: float | None = None,
    deck_desired_retention: float | None = None,
    rpc: _RwkvQueueScoreRpc | None = None,
    historical_review_rows: (list[tuple[object, ...]] | None) = None,
) -> SimpleNamespace:
    if historical_review_rows is not None:
        historical_review_rows = _benchmark_valid_historical_rows(
            historical_review_rows
        )
    states = SchedulingStates()
    states.current.normal.review.elapsed_days = 7

    class Scheduler:
        def __init__(self) -> None:
            self.states = states

        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return self.states

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return self._config(deck_id)

        def all_config(self) -> list[dict[str, object]]:
            return [self._config(10)]

        def _config(self, deck_id: int) -> dict[str, object]:
            config: dict[str, object] = {
                "id": deck_id * 10,
                "rwkvReviewEnabled": rwkv_review_enabled,
                "rwkvReviewEnforceGradeOrder": rwkv_review_enforce_grade_order,
                "rwkvReviewInstantOrderEnabled": (rwkv_review_instant_order_enabled),
                "rwkvReviewDynamicPresetReplay": rwkv_review_dynamic_preset_replay,
                "rwkvReviewFirstReviewElapsedFromCardCreation": (
                    rwkv_review_first_review_elapsed_from_card_creation
                ),
            }
            if deck_desired_retention is not None:
                config["desiredRetention"] = deck_desired_retention
            return config

    col = SimpleNamespace(
        sched=Scheduler(),
        decks=Decks(),
    )
    if rpc is not None:
        col._backend = rpc
    if historical_review_rows is not None:

        class DB:
            def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
                if sql == "select cid, count() from revlog group by cid order by cid":
                    # the card-id ranges a split whole-history query runs in
                    # (spec sched.rwkv-startup-no-window)
                    counts: dict[int, int] = {}
                    for row in historical_review_rows or []:
                        counts[cast(int, row[1])] = counts.get(cast(int, row[1]), 0) + 1
                    return sorted(counts.items())
                assert "from revlog r" in sql
                assert "join cards c" in sql
                assert args == ()
                card_range = re.search(r"and r\.cid between (\d+) and (\d+)", sql)
                if card_range is None:
                    return historical_review_rows
                low, high = int(card_range[1]), int(card_range[2])
                return [
                    row
                    for row in (historical_review_rows or [])
                    if low <= cast(int, row[1]) <= high
                ]

            def execute(self, sql: str) -> None:
                pytest.fail(f"unexpected DB execute: {sql}")

            def executemany(
                self,
                sql: str,
                rows: list[tuple[int, float, str, int, str, int]],
            ) -> None:
                pytest.fail(f"unexpected DB executemany: {sql}, {rows}")

        col.db = DB()
    if resolved_preset_id is not None:
        col.fsrs_preset_for_card = lambda card_id: SimpleNamespace(
            id=resolved_preset_id,
            desired_retention=preset_desired_retention,
        )

    return SimpleNamespace(
        _v3=SimpleNamespace(states=states),
        mw=SimpleNamespace(col=col),
    )


def _rwkv_cache_reviewer(
    *,
    profile_folder: Path,
    rows: list[tuple[int, ...]],
    deck_config_overrides: dict[str, object] | None = None,
) -> SimpleNamespace:
    rows[:] = cast(list[tuple[int, ...]], _benchmark_valid_historical_rows(rows))
    states = SchedulingStates()
    rwkv_retrievability_rows: list[tuple[int, float, str, int, str, int]] = []
    review_prediction_rows: list[tuple[int, int, float, str, str]] = []
    saved_deck_configs: list[dict[str, object]] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[int, ...]]:
            if "PRAGMA table_info" in sql:
                if "revlog" in sql:
                    return [
                        (0, "id"),
                        (1, "cid"),
                        (2, "ease"),
                        (3, "ivl"),
                        (4, "lastIvl"),
                        (5, "time"),
                        (6, "factor"),
                        (7, "type"),
                    ]
                return [
                    (0, "revlog_id"),
                    (1, "prediction"),
                    (2, "source"),
                    (3, "updated_at"),
                    (4, "sample_role"),
                    (5, "fold_index"),
                ]
            if "FROM search_stats_rwkv_review_retrievability cache" in sql:
                requested_ids = _sql_integers_inside_first_in_clause(sql)
                return [
                    (review_id, prediction)
                    for review_id, prediction, *_ in rwkv_retrievability_rows
                    if review_id in requested_ids
                ]
            if sql == "select cid, count() from revlog group by cid order by cid":
                # the card-id ranges a split whole-history query runs in
                # (spec sched.rwkv-startup-no-window)
                counts: dict[int, int] = {}
                for row in rows:
                    counts[row[1]] = counts.get(row[1], 0) + 1
                return sorted(counts.items())
            assert "from revlog r" in sql
            assert "join cards c" in sql
            # Normalize here too, not only at fixture time: a test may append a
            # raw row after the reviewer is built, and the query it stands in
            # for always returns the `is_learning_start` column.
            query_rows = cast(
                list[tuple[int, ...]], _benchmark_valid_historical_rows(rows)
            )
            card_range = re.search(r"and r\.cid between (\d+) and (\d+)", sql)
            if card_range is not None:
                low, high = int(card_range[1]), int(card_range[2])
                query_rows = [row for row in query_rows if low <= row[1] <= high]
            if args:
                assert len(args) == 1
                after_review_id = args[0]
                assert isinstance(after_review_id, int)
                return [row for row in query_rows if row[0] > after_review_id]
            return query_rows

        def scalar(self, sql: str, *args: object) -> int | None:
            if "select crt from col" in sql:
                assert args == ()
                return 12345
            if "sample_role =" in sql:
                assert len(args) == 2
                last_review_id, sample_role = args
                assert isinstance(last_review_id, int)
                return next(
                    (
                        1
                        for review_id, prediction, _, _, row_sample_role, _ in rwkv_retrievability_rows
                        if review_id <= last_review_id
                        and 0 <= prediction <= 1
                        and row_sample_role == sample_role
                    ),
                    None,
                )
            assert "from revlog" in sql
            assert len(args) == 1
            if "order by id desc" in sql:
                card_id = args[0]
                assert isinstance(card_id, int)
                review_ids = [
                    row[0]
                    for row in rows
                    if row[1] == card_id
                    and 1 <= row[4] <= 4
                    and (row[6] in (0, 1, 2, 3) or row[6] == 4)
                ]
                return max(review_ids, default=None)

            last_review_id = args[0]
            assert isinstance(last_review_id, int)
            if "search_stats_rwkv_review_retrievability" in sql:
                review_ids = {
                    row[0]
                    for row in rows
                    if row[0] <= last_review_id
                    and 1 <= row[4] <= 4
                    and (row[6] in (0, 1, 2, 3) or row[6] == 4)
                }
                return sum(
                    1
                    for review_id, prediction, *_ in rwkv_retrievability_rows
                    if review_id in review_ids and 0 <= prediction <= 1
                )
            return sum(1 for row in rows if row[0] <= last_review_id)

        def execute(self, sql: str) -> None:
            pytest.fail(f"unexpected DB execute: {sql}")

        def executemany(
            self,
            sql: str,
            cache_rows: list[tuple[int, float, str, int, str, int]],
        ) -> None:
            pytest.fail(f"unexpected DB executemany: {sql}, {cache_rows}")

    class Backend:
        def set_rwkv_review_retrievability_cache_rows(
            self,
            *,
            source: str,
            rows: Sequence[object],
        ) -> None:
            existing = {row[0]: row for row in rwkv_retrievability_rows}
            for row in rows:
                review_id = getattr(row, "revlog_id")
                prediction = getattr(row, "prediction")
                sample_role = getattr(row, "sample_role", "final_fit")
                fold_index = getattr(row, "fold_index", -1)
                existing[review_id] = (
                    review_id,
                    prediction,
                    source,
                    0,
                    sample_role,
                    fold_index,
                )
            rwkv_retrievability_rows[:] = [
                existing[review_id] for review_id in sorted(existing)
            ]

        def set_review_predictions(
            self,
            *,
            algorithm: int,
            source: str,
            rows: Sequence[object],
        ) -> None:
            for row in rows:
                review_prediction_rows.append(
                    (
                        int(algorithm),
                        getattr(row, "revlog_id"),
                        getattr(row, "prediction"),
                        source,
                        getattr(row, "sample_role", "final_fit"),
                    )
                )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return states

    class Decks:
        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=100)]

        def all_config(self) -> list[dict[str, object]]:
            return [self.config_dict_for_deck_id(100)]

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100]

        def get_config(self, config_id: int) -> dict[str, object] | None:
            if config_id == 1000:
                return self.config_dict_for_deck_id(100)
            return None

        def update_config(self, config: dict[str, object]) -> None:
            saved_deck_configs.append(config)

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            config: dict[str, object] = {
                "id": deck_id * 10,
                "rwkvReviewEnabled": True,
            }
            if deck_config_overrides is not None:
                config.update(deck_config_overrides)
            return config

    col = SimpleNamespace(
        db=DB(),
        sched=Scheduler(),
        decks=Decks(),
        _backend=Backend(),
        path=str(profile_folder / "collection.anki2"),
        fsrs_preset_for_card=lambda card_id: SimpleNamespace(id="1000"),
        rwkv_retrievability_rows=rwkv_retrievability_rows,
        review_prediction_rows=review_prediction_rows,
        saved_deck_configs=saved_deck_configs,
    )
    mw = SimpleNamespace(
        col=col,
        pm=SimpleNamespace(profileFolder=lambda: str(profile_folder)),
    )
    return SimpleNamespace(_v3=SimpleNamespace(states=states), mw=mw)


def _benchmark_valid_historical_rows(
    rows: Sequence[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """Rows shaped the way the replay query returns them.

    Each card's first row is made a Learning row and flagged
    `is_learning_start`, which is the tenth column the query itself computes
    (`sched.rwkv-replay-start-row`). These rows stand in for the query's
    output; the rule itself is pinned by the tests that run the real SQL.
    """
    seen_cards: set[int] = set()
    normalized: list[tuple[object, ...]] = []
    for row in rows:
        if len(row) < 7 or not isinstance(row[1], int):
            normalized.append(row)
            continue
        card_id = row[1]
        body = row[:9] if len(row) >= 9 else row
        if card_id in seen_cards:
            normalized.append((*body, 0))
            continue
        seen_cards.add(card_id)
        normalized.append((*body[:6], 0, *body[7:], 1))
    return normalized


def _rwkv_sse_harness_review_retrievability(
    col: SimpleNamespace,
    revlog_ids: list[int],
) -> dict[str, object]:
    requested_review_ids = list(dict.fromkeys(revlog_ids))
    if not requested_review_ids:
        return {"column": None, "data": []}

    table = "search_stats_rwkv_review_retrievability"
    reviews = ",".join(str(revlog_id) for revlog_id in requested_review_ids)
    predictions_by_revlog_id = {
        row[0]: row[1]
        for row in col.db.all(f"""
        SELECT cache.revlog_id, cache.prediction
        FROM {table} cache
        WHERE cache.revlog_id IN ({reviews})
        ORDER BY cache.revlog_id
        """)
        if row[0] in requested_review_ids
        and _rwkv_sse_harness_valid_probability(row[1])
    }

    if set(requested_review_ids) - set(predictions_by_revlog_id):
        return {"column": None, "data": []}

    return {
        "column": table,
        "data": sorted(predictions_by_revlog_id.items()),
    }


def _rwkv_sse_harness_valid_probability(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )


def _sql_integers_inside_first_in_clause(sql: str) -> set[int]:
    _, _, after_in = sql.partition(" IN (")
    inside, _, _ = after_in.partition(")")
    return {int(value.strip()) for value in inside.split(",") if value.strip()}


def _attach_progress_taskman(
    mw: SimpleNamespace,
) -> tuple[Any, list[dict[str, object]]]:
    progress_updates: list[dict[str, object]] = []

    class Progress:
        def update(self, **kwargs: object) -> None:
            progress_updates.append(kwargs)

    class Taskman:
        def __init__(self) -> None:
            self.with_progress_kwargs: dict[str, object] | None = None
            self.background_runs = 0

        def run_on_main(self, callback: object) -> None:
            assert callable(callback)
            callback()

        def _run(self, task: object, on_done: object) -> None:
            assert callable(task)
            assert callable(on_done)
            future: Future[Any] = Future()
            try:
                future.set_result(task())
            except Exception as exc:
                future.set_exception(exc)
            on_done(future)

        def with_progress(
            self,
            task: object,
            on_done: object,
            **kwargs: object,
        ) -> None:
            self.with_progress_kwargs = kwargs
            self._run(task, on_done)

        def run_in_background(
            self,
            task: object,
            on_done: object,
            *,
            uses_collection: bool = True,
        ) -> None:
            self.background_runs += 1
            self._run(task, on_done)

    taskman = Taskman()
    mw.taskman = taskman
    mw.progress = Progress()
    mw.inMainThread = lambda: True
    return taskman, progress_updates


def _expected_preset_hash(preset_id: str) -> int:
    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _rwkv_card(
    *,
    card_id: int,
    note_id: int,
    duration_millis: int,
    deck_id: int = 100,
    last_review_time: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=card_id,
        nid=note_id,
        did=deck_id,
        type=2,
        queue=2,
        due=50,
        ivl=4,
        factor=2500,
        reps=5,
        lapses=1,
        last_review_time=last_review_time,
        time_taken=lambda capped=True: duration_millis,
    )


def _rwkv_review_input(*, card_id: int, note_id: int) -> RwkvReviewInput:
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card_id,
            note_id=note_id,
            deck_id=100,
            preset_id=1000,
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=2,
        card_queue=2,
        card_due=50,
        interval_days=4,
        ease_factor=2500,
        reps=5,
        lapses=1,
        day_offset=42,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=7,
        current_elapsed_seconds=604800,
    )


class _MultiBatchStateTokenBackend:
    def __init__(self) -> None:
        self.generation = 0
        self.prediction_calls: list[list[int]] = []

    def warm_up(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("test warm-up is pre-seeded")

    def state_generation(self) -> int:
        return self.generation

    def cached_review_input_predictions(
        self,
        inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    ) -> tuple[
        list[RwkvReviewPrediction | None],
        list[tuple[int, RwkvReviewPredictionRequest]],
        int,
    ]:
        return (
            [None] * len(inputs_by_index),
            [
                (
                    index,
                    RwkvReviewPredictionRequest(review_input=review_input),
                )
                for index, review_input in inputs_by_index
            ],
            0,
        )

    def predict_review_requests(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> list[RwkvReviewPrediction]:
        self.prediction_calls.append(
            [request.review_input.identity.card_id for request in requests]
        )
        return [
            RwkvReviewPrediction(
                retrievability=0.60,
                current_interval=10,
                current_s90=20,
            )
            for _ in requests
        ]


def _mutate_multi_batch_prediction_state(
    mutation: str,
    *,
    reviewer: object,
    backend_a: _MultiBatchStateTokenBackend,
    backend_b: _MultiBatchStateTokenBackend,
) -> None:
    if mutation == "backend_swap":
        set_reviewer_backend(backend_b)
    elif mutation == "same_backend_reset":
        set_reviewer_backend(backend_a)
    elif mutation == "backend_aba":
        set_reviewer_backend(backend_b)
        set_reviewer_backend(backend_a)
    elif mutation == "runtime_generation":
        backend_a.generation += 1
    elif mutation == "input_epoch":
        with rwkv_scheduler._reviewer_backend_state_lock:
            rwkv_scheduler._rwkv_review_input_generation += 1
    elif mutation == "study_epoch":
        with rwkv_scheduler._reviewer_backend_state_lock:
            rwkv_scheduler._rwkv_study_queue_generation += 1
    else:
        assert mutation == "resident_invalidation"
        rwkv_scheduler._invalidate_reviewer_backend_state(
            reviewer,
            reason="multi-batch test",
        )


def _start_multi_batch_execution_lock_holder(
    release: threading.Event,
) -> threading.Thread:
    acquired = threading.Event()

    def hold() -> None:
        rwkv_scheduler._reviewer_backend_execution_lock.acquire()
        try:
            acquired.set()
            assert release.wait(timeout=5)
        finally:
            rwkv_scheduler._reviewer_backend_execution_lock.release()

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(timeout=5)
    return thread


@pytest.mark.parametrize(
    "mutation",
    (
        "backend_swap",
        "same_backend_reset",
        "backend_aba",
        "runtime_generation",
        "input_epoch",
        "study_epoch",
        "resident_invalidation",
        "collection_swap",
        "collection_backend_swap",
        "contention",
    ),
)
def test_stats_multi_batch_discards_scores_after_prediction_state_change(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    backend_a = _MultiBatchStateTokenBackend()
    backend_b = _MultiBatchStateTokenBackend()
    rpc = _RwkvQueueScoreRpc()
    replacement_rpc = _RwkvQueueScoreRpc()
    col = SimpleNamespace(
        _backend=rpc,
        db=object(),
        sched=SimpleNamespace(
            _timing_today=lambda: SimpleNamespace(
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )
        ),
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={
            64: [(1, _rwkv_review_input(card_id=1, note_id=101))],
            128: [(2, _rwkv_review_input(card_id=2, note_id=102))],
        },
        loaded_rows=2,
        parsed_cards=2,
        cards_with_state=2,
        disabled_config_cards=0,
        eligible_cards=2,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_search",
        lambda **kwargs: input_build,
    )

    original_predict = rwkv_scheduler._rwkv_review_scores_for_inputs
    predict_calls = 0
    lock_release = threading.Event()
    lock_thread: threading.Thread | None = None

    def predict(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        *,
        batch_size: int,
        state_token: (
            rwkv_scheduler._ReviewerBackendPredictionStateToken | None
        ) = None,
    ) -> list[tuple[int, float]] | None:
        nonlocal lock_thread, predict_calls
        second_contended_batch = mutation == "contention" and predict_calls == 1
        try:
            result = original_predict(
                inputs_by_card_id,
                batch_size=batch_size,
                state_token=state_token,
            )
        finally:
            if second_contended_batch:
                lock_release.set()
                assert lock_thread is not None
                lock_thread.join(timeout=5)
                assert not lock_thread.is_alive()
        predict_calls += 1
        if predict_calls == 1:
            if mutation == "contention":
                lock_thread = _start_multi_batch_execution_lock_holder(lock_release)
            elif mutation == "collection_swap":
                reviewer.mw.col = SimpleNamespace(
                    _backend=replacement_rpc,
                    db=object(),
                    sched=col.sched,
                )
            elif mutation == "collection_backend_swap":
                col._backend = replacement_rpc
            else:
                _mutate_multi_batch_prediction_state(
                    mutation,
                    reviewer=reviewer,
                    backend_a=backend_a,
                    backend_b=backend_b,
                )
        return result

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_scores_for_inputs",
        predict,
    )

    previous_backend = set_reviewer_backend(backend_a)
    resident_key = (id(backend_a), id(col))
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    try:
        status = prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        lock_release.set()
        if lock_thread is not None:
            lock_thread.join(timeout=5)
            assert not lock_thread.is_alive()
        set_reviewer_backend(previous_backend)

    expected_status = (
        rwkv_scheduler.RwkvStatsPreparationStatus.PENDING
        if mutation == "contention"
        else rwkv_scheduler.RwkvStatsPreparationStatus.FAILED
    )
    assert status == expected_status
    assert rpc.stats_calls == []
    assert replacement_rpc.stats_calls == []
    assert backend_a.prediction_calls == [[1]]
    assert backend_b.prediction_calls == []


def test_stats_single_flight_key_changes_after_same_backend_reset() -> None:
    backend = cast(Any, SimpleNamespace(state_generation=lambda: 0))
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=object(),
                sched=SimpleNamespace(
                    _timing_today=lambda: SimpleNamespace(
                        days_elapsed=42,
                        next_day_at=43 * 86_400,
                    )
                ),
            )
        )
    )

    set_reviewer_backend(backend)
    first_token = rwkv_scheduler._capture_reviewer_backend_prediction_state_token(
        reviewer,
        expected_backend=backend,
    )
    assert first_token is not None
    first_key = rwkv_scheduler._rwkv_stats_prepare_key(
        reviewer,
        "rated:7",
        state_token=first_token,
    )

    set_reviewer_backend(backend)
    second_token = rwkv_scheduler._capture_reviewer_backend_prediction_state_token(
        reviewer,
        expected_backend=backend,
    )
    assert second_token is not None
    second_key = rwkv_scheduler._rwkv_stats_prepare_key(
        reviewer,
        "rated:7",
        state_token=second_token,
    )

    assert first_key is not None
    assert second_key is not None
    assert first_key != second_key
    assert not rwkv_scheduler._reviewer_backend_prediction_state_token_is_current(
        first_token
    )


def test_tokenized_candidate_prediction_aborts_on_contention() -> None:
    prediction_calls = 0

    def predict_reviews(
        candidates: Sequence[RwkvReviewCandidate],
    ) -> list[RwkvReviewPrediction]:
        nonlocal prediction_calls
        prediction_calls += 1
        return [RwkvReviewPrediction(retrievability=0.5) for _ in candidates]

    backend = cast(
        Any,
        SimpleNamespace(
            predict_reviews=predict_reviews,
            state_generation=lambda: 0,
        ),
    )
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=_RwkvQueueScoreRpc(),
                db=object(),
            )
        )
    )
    set_reviewer_backend(backend)
    state_token = rwkv_scheduler._capture_reviewer_backend_prediction_state_token(
        reviewer,
        expected_backend=backend,
    )
    assert state_token is not None

    release = threading.Event()
    thread = _start_multi_batch_execution_lock_holder(release)
    try:
        with pytest.raises(rwkv_scheduler._ReviewerBackendPredictionAborted):
            rwkv_scheduler._predict_review_batch(
                [
                    RwkvReviewCandidate(
                        reviewer=reviewer,
                        card=SimpleNamespace(id=1),
                    )
                ],
                state_token=state_token,
            )
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert prediction_calls == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "backend_swap",
        "same_backend_reset",
        "backend_aba",
        "runtime_generation",
        "input_epoch",
        "study_epoch",
        "resident_invalidation",
        "collection_swap",
        "collection_backend_swap",
        "contention",
    ),
)
def test_reschedule_multi_batch_discards_items_after_prediction_state_change(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    class Rpc:
        def __init__(self) -> None:
            self.apply_calls: list[bytes] = []

        def apply_rwkv_review_reschedule_raw(self, message: bytes) -> bytes:
            self.apply_calls.append(message)
            return b""

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    backend_a = _MultiBatchStateTokenBackend()
    backend_b = _MultiBatchStateTokenBackend()
    rpc = Rpc()
    replacement_rpc = Rpc()
    col = SimpleNamespace(
        _backend=rpc,
        db=object(),
        decks=Decks(),
    )
    mw = SimpleNamespace(col=col)
    reviewer = SimpleNamespace(mw=mw)
    inputs = [
        (
            card_id,
            _rwkv_review_input(card_id=card_id, note_id=card_id + 1000),
        )
        for card_id in range(1, 130)
    ]
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={8192: inputs},
        loaded_rows=len(inputs),
        parsed_cards=len(inputs),
        cards_with_state=len(inputs),
        disabled_config_cards=0,
        eligible_cards=len(inputs),
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "warm_up_rwkv_state",
        lambda mw, *, progress=None: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_deck_review_queue",
        lambda **kwargs: input_build,
    )

    original_predict = rwkv_scheduler._rwkv_review_predictions_for_inputs
    predict_calls = 0
    lock_release = threading.Event()
    lock_thread: threading.Thread | None = None

    def predict(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        *,
        batch_size: int,
        state_token: (
            rwkv_scheduler._ReviewerBackendPredictionStateToken | None
        ) = None,
    ) -> list[RwkvReviewPrediction | None] | None:
        nonlocal lock_thread, predict_calls
        second_contended_batch = mutation == "contention" and predict_calls == 1
        try:
            result = original_predict(
                inputs_by_card_id,
                batch_size=batch_size,
                state_token=state_token,
            )
        finally:
            if second_contended_batch:
                lock_release.set()
                assert lock_thread is not None
                lock_thread.join(timeout=5)
                assert not lock_thread.is_alive()
        predict_calls += 1
        if predict_calls == 1:
            if mutation == "contention":
                lock_thread = _start_multi_batch_execution_lock_holder(lock_release)
            elif mutation == "collection_swap":
                mw.col = SimpleNamespace(
                    _backend=replacement_rpc,
                    db=object(),
                    decks=Decks(),
                )
            elif mutation == "collection_backend_swap":
                col._backend = replacement_rpc
            else:
                _mutate_multi_batch_prediction_state(
                    mutation,
                    reviewer=reviewer,
                    backend_a=backend_a,
                    backend_b=backend_b,
                )
        return result

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_predictions_for_inputs",
        predict,
    )

    previous_backend = set_reviewer_backend(backend_a)
    resident_key = (id(backend_a), id(col))
    rwkv_scheduler._reviewer_backend_warmup_states[resident_key] = None
    try:
        result = rwkv_scheduler.reschedule_rwkv_review_cards(mw, deck_id=100)
    finally:
        lock_release.set()
        if lock_thread is not None:
            lock_thread.join(timeout=5)
            assert not lock_thread.is_alive()
        set_reviewer_backend(previous_backend)

    assert result.built is False
    assert result.changes is None
    assert rpc.apply_calls == []
    assert replacement_rpc.apply_calls == []
    assert backend_a.prediction_calls == [list(range(1, 129))]
    assert backend_b.prediction_calls == []


def _rwkv_async_test_work(
    backend: object,
    *,
    state_generation: int = 0,
) -> rwkv_scheduler.RwkvReviewQueueOrderAsyncWork:
    review_input = _rwkv_review_input(card_id=1, note_id=10)
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, review_input)]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    return rwkv_scheduler.RwkvReviewQueueOrderAsyncWork(
        context=rwkv_scheduler.RwkvReviewQueueContext(
            collection_key=(1, 2),
            selected_deck_id=100,
            deck_id=100,
            deck_scope=(100,),
            days_elapsed=42,
            next_day_at=43 * 86_400,
            config_key="",
            review_input_generation=0,
            study_queue_generation=0,
        ),
        deck_id=100,
        reason="review queue",
        batch_size=512,
        state_generation=state_generation,
        input_build=input_build,
        inputs_by_card_id=((1, review_input),),
        predictions=(None,),
        requests_by_index=(),
        resident_inputs_by_index=((0, review_input),),
        cache_hits=0,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        backend=cast(Any, backend),
    )


def _rwkv_checkpoint_test_history(
    review_count: int,
) -> rwkv_scheduler.RwkvHistoricalReviewInputs:
    reviews = [
        replace(
            _rwkv_review_input(
                card_id=(index % 2) + 1,
                note_id=10 + (index % 2),
            ),
            is_query=False,
            ease=(index % 4) + 1,
            interval_days=index + 1,
        )
        for index in range(review_count)
    ]
    review_ids = [1_000 + index * 1_000 for index in range(review_count)]
    previous_ids: dict[int, int] = {}
    previous_intervals: dict[int, int] = {}
    review_counts: dict[int, int] = {}
    history_hash = rwkv_scheduler._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    for review_id, review in zip(review_ids, reviews, strict=True):
        card_id = review.identity.card_id
        previous_ids[card_id] = review_id
        if review.interval_days is not None:
            previous_intervals[card_id] = review.interval_days
        review_counts[card_id] = review_counts.get(card_id, 0) + 1
        history_hash = rwkv_scheduler._rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )

    return rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=review_ids[-1],
        review_count=review_count,
        history_hash=history_hash,
        replay_key="checkpoint-replay",
    )


def _assert_rwkv_checkpoint_history_matches(
    actual: rwkv_scheduler.RwkvHistoricalReviewInputs,
    expected: rwkv_scheduler.RwkvHistoricalReviewInputs,
) -> None:
    assert actual.reviews == []
    assert actual.review_ids == []
    assert actual.previous_review_id_by_card == expected.previous_review_id_by_card
    assert (
        actual.previous_interval_days_by_card == expected.previous_interval_days_by_card
    )
    assert actual.review_count_by_card == expected.review_count_by_card
    assert actual.last_review_id == expected.last_review_id
    assert actual.review_count == expected.review_count
    assert actual.history_hash == expected.history_hash
    assert actual.replay_key == expected.replay_key


def _rwkv_resident_identity(
    *,
    last_review_id: int = 2000,
    review_count: int = 2,
    history_hash: str = "c" * 64,
    replay_key: str = "canonical-replay",
) -> rwkv_scheduler.RwkvResidentStateIdentity:
    return rwkv_scheduler.RwkvResidentStateIdentity(
        last_review_id=last_review_id,
        review_count=review_count,
        history_hash=history_hash,
        replay_key=replay_key,
    )


def _rwkv_canonical_history() -> rwkv_scheduler.RwkvHistoricalReviewInputs:
    return rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card={},
        previous_interval_days_by_card={},
        review_count_by_card={},
        last_review_id=2000,
        review_count=2,
        history_hash="d" * 64,
        replay_key="canonical-replay",
    )


def _rwkv_memorised_test_identity(
    *,
    day_offset: int,
    review_ids: Sequence[int],
    reviews: Sequence[RwkvReviewInput],
    model: str = "same",
    replay_key: str = "same-replay",
) -> str:
    history_hash = rwkv_scheduler._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    for review_id, review in zip(review_ids, reviews, strict=True):
        history_hash = rwkv_scheduler._rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )
    return json.dumps(
        {
            "version": 3,
            "model": model,
            "replayKey": replay_key,
            "historyHash": history_hash,
            "dayOffset": day_offset,
            "lastReviewId": review_ids[-1] if review_ids else 0,
            "reviewCount": len(review_ids),
        },
        sort_keys=True,
    )


def _normal_review_state(interval: int, fuzz_delta: int) -> SchedulingState:
    state = SchedulingState()
    state.normal.review.scheduled_days = interval
    state.normal.review.fuzz_delta_days = fuzz_delta
    return state


def _learning_state() -> SchedulingState:
    state = SchedulingState()
    state.normal.learning.scheduled_secs = 60
    return state


def _relearning_state() -> SchedulingState:
    state = SchedulingState()
    state.normal.relearning.review.scheduled_days = 2
    state.normal.relearning.learning.scheduled_secs = 120
    return state


def _filtered_preview_state() -> SchedulingState:
    state = SchedulingState()
    state.filtered.preview.scheduled_secs = 180
    return state


# ---- deck-options.reschedule-on-change --------------------------------------


def _reschedule_request(
    *,
    configs: Sequence[tuple[int, float, bool] | tuple[int, float, bool, bool]] = (),
    deck_desired_retention: float | None = None,
) -> deck_config_pb2.UpdateDeckConfigsRequest:
    request = deck_config_pb2.UpdateDeckConfigsRequest()
    request.target_deck_id = 1
    for entry in configs:
        config_id, desired_retention, curve = entry[:3]
        instant = bool(entry[3]) if len(entry) > 3 else False
        config = request.configs.add()
        config.id = config_id
        config.config.desired_retention = desired_retention
        config.config.rwkv_review_enabled = curve
        config.config.rwkv_review_instant_order_enabled = instant
    if deck_desired_retention is not None:
        request.limits.desired_retention = deck_desired_retention
    return request


def _reschedule_snapshot(
    presets: dict[int, tuple[float, bool] | tuple[float, bool, bool]],
    deck_desired_retention: float | None = None,
    *,
    reschedule_on_change: bool = True,
) -> rwkv_scheduler.RwkvCurveRescheduleSnapshot:
    return rwkv_scheduler.RwkvCurveRescheduleSnapshot(
        preset_desired_retention={k: v[0] for k, v in presets.items()},
        preset_curve_enabled={k: v[1] for k, v in presets.items()},
        deck_desired_retention=deck_desired_retention,
        preset_instant_enabled={
            k: bool(v[2]) if len(v) > 2 else False for k, v in presets.items()
        },
        reschedule_on_change=reschedule_on_change,
    )


def test_reschedule_snapshot_reads_the_stored_reschedule_choice() -> None:
    """Pins spec/deck-options.md#deck-options.collection-wide-in-preferences."""

    def make_mw(stored: bool) -> SimpleNamespace:
        def get_config(key: str, default: object = None) -> object:
            assert key == "fsrsReschedule"
            return stored

        return SimpleNamespace(
            col=SimpleNamespace(decks=SimpleNamespace(), get_config=get_config)
        )

    request = _reschedule_request(configs=[(10, 0.85, True)])
    assert rwkv_scheduler.rwkv_curve_reschedule_snapshot(
        make_mw(True), request
    ).reschedule_on_change
    assert not rwkv_scheduler.rwkv_curve_reschedule_snapshot(
        make_mw(False), request
    ).reschedule_on_change
    # no collection at all: the choice reads as off
    assert not rwkv_scheduler.rwkv_curve_reschedule_snapshot(
        SimpleNamespace(col=None), request
    ).reschedule_on_change


def test_rwkv_curve_reschedule_needed_when_curve_preset_retention_changes() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, True)})
    assert rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot, _reschedule_request(configs=[(10, 0.85, True)])
    )
    assert not rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot, _reschedule_request(configs=[(10, 0.9, True)])
    )


def test_rwkv_curve_reschedule_not_needed_without_the_switch() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, True)}, reschedule_on_change=False)
    assert not rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot,
        _reschedule_request(configs=[(10, 0.85, True)]),
    )


def test_rwkv_curve_reschedule_ignores_presets_without_curve() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, False), 11: (0.9, True)})
    assert not rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot,
        _reschedule_request(configs=[(11, 0.9, True), (10, 0.8, False)]),
    )


def test_rwkv_curve_reschedule_needed_when_preset_becomes_curve() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, False)})
    assert rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot, _reschedule_request(configs=[(10, 0.9, True)])
    )
    # A preset the collection did not know (new preset) counts as newly Curve.
    assert rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot, _reschedule_request(configs=[(0, 0.9, True)])
    )


def test_rwkv_curve_reschedule_needed_when_deck_override_changes() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, True)}, deck_desired_retention=0.9)
    assert rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot,
        _reschedule_request(configs=[(10, 0.9, True)], deck_desired_retention=0.8),
    )
    assert rwkv_scheduler.rwkv_curve_reschedule_needed(
        snapshot, _reschedule_request(configs=[(10, 0.9, True)])
    )
    # The override only matters for the preset the deck ends up with.
    assert not rwkv_scheduler.rwkv_curve_reschedule_needed(
        _reschedule_snapshot({10: (0.9, True), 11: (0.9, False)}, 0.9),
        _reschedule_request(
            configs=[(10, 0.9, True), (11, 0.9, False)],
            deck_desired_retention=0.8,
        ),
    )


def test_rwkv_curve_reschedule_snapshot_reads_legacy_dicts() -> None:
    class Decks:
        def get_config(self, config_id: int) -> dict[str, object] | None:
            return {
                10: {
                    "desiredRetention": 0.9,
                    "other": {"jschoreels.rwkv": {"rwkv_review_enabled": True}},
                },
                11: {"desiredRetention": 0.85},
            }.get(config_id)

        def get(self, deck_id: int, default: bool = True) -> dict[str, object] | None:
            return {"id": deck_id, "desiredRetention": 80} if deck_id == 1 else None

    mw = SimpleNamespace(col=SimpleNamespace(decks=Decks()))
    snapshot = rwkv_scheduler.rwkv_curve_reschedule_snapshot(
        mw,
        _reschedule_request(
            configs=[(10, 0.9, True), (11, 0.85, False), (12, 0.9, False)]
        ),
    )
    assert snapshot.preset_desired_retention == {10: 0.9, 11: 0.85}
    assert snapshot.preset_curve_enabled == {10: True, 11: False}
    assert snapshot.preset_instant_enabled == {10: False, 11: False}
    assert snapshot.deck_desired_retention == pytest.approx(0.8)


def test_reschedule_rwkv_curve_after_save_runs_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "reschedule_rwkv_review_cards_with_progress",
        lambda _mw, *, deck_id=None: calls.append(deck_id),
    )
    mw = SimpleNamespace()
    snapshot = _reschedule_snapshot({10: (0.9, True)})
    assert not rwkv_scheduler.reschedule_rwkv_curve_after_save(
        mw, snapshot, _reschedule_request(configs=[(10, 0.9, True)])
    )
    assert calls == []
    assert rwkv_scheduler.reschedule_rwkv_curve_after_save(
        mw, snapshot, _reschedule_request(configs=[(10, 0.85, True)])
    )
    assert calls == [None]


def test_rwkv_instant_refresh_needed_when_instant_retention_changes() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, False, True)})
    assert rwkv_scheduler.rwkv_instant_refresh_needed(
        snapshot, _reschedule_request(configs=[(10, 0.85, False, True)])
    )
    assert not rwkv_scheduler.rwkv_instant_refresh_needed(
        snapshot, _reschedule_request(configs=[(10, 0.9, False, True)])
    )
    assert not rwkv_scheduler.rwkv_instant_refresh_needed(
        _reschedule_snapshot({10: (0.9, False, True)}, reschedule_on_change=False),
        _reschedule_request(configs=[(10, 0.85, False, True)]),
    )
    # An RWKV-Curve change is not an RWKV-Instant change.
    assert not rwkv_scheduler.rwkv_instant_refresh_needed(
        _reschedule_snapshot({10: (0.9, True)}),
        _reschedule_request(configs=[(10, 0.85, True)]),
    )


def test_rwkv_instant_refresh_needed_when_preset_becomes_instant() -> None:
    snapshot = _reschedule_snapshot({10: (0.9, False, False)})
    assert rwkv_scheduler.rwkv_instant_refresh_needed(
        snapshot, _reschedule_request(configs=[(10, 0.9, False, True)])
    )


def test_refresh_rwkv_instant_after_save_invalidates_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidated: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_invalidate_rwkv_review_input_caches",
        lambda mw: invalidated.append(mw) or 7,
    )
    resets: list[int] = []
    mw = SimpleNamespace(reset=lambda: resets.append(1))
    snapshot = _reschedule_snapshot({10: (0.9, False, True)})
    assert not rwkv_scheduler.refresh_rwkv_instant_after_save(
        mw, snapshot, _reschedule_request(configs=[(10, 0.9, False, True)])
    )
    assert invalidated == [] and resets == []
    assert rwkv_scheduler.refresh_rwkv_instant_after_save(
        mw, snapshot, _reschedule_request(configs=[(10, 0.8, False, True)])
    )
    assert invalidated == [mw] and resets == [1]


class _CardCurveBackend:
    def __init__(self, result: tuple[list[float], float] | None) -> None:
        self.result = result
        self.calls: list[tuple[int, tuple[float, ...]]] = []

    def card_curve(
        self, card_id: int, elapsed_days: Sequence[float]
    ) -> tuple[list[float], float] | None:
        self.calls.append((card_id, tuple(elapsed_days)))
        return self.result


def _card_curve_ready(
    monkeypatch: pytest.MonkeyPatch, backend: object, *, curve_preset: bool = True
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def access(**_kwargs: Any) -> Iterator[object]:
        yield backend

    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler, "rwkv_review_enabled", lambda reviewer, card: curve_preset
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_card_info", lambda reviewer: True
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_capture_reviewer_backend_prediction_state_token",
        lambda reviewer, expected_backend: object(),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_try_reviewer_backend_prediction_access", access
    )


def test_rwkv_card_info_curve_samples_the_stored_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve"""
    grid = rwkv_scheduler.RWKV_CARD_INFO_CURVE_DAYS
    backend = _CardCurveBackend(([1.0 - day / 1e5 for day in grid], 3.25))
    _card_curve_ready(monkeypatch, backend)

    curve = rwkv_scheduler.rwkv_card_info_curve(object(), SimpleNamespace(id=42))

    assert curve is not None
    assert backend.calls == [(42, grid)]
    assert curve.elapsed_days == grid
    assert curve.recall[0] == 1.0 and curve.s90 == 3.25
    # 0, then one minute to 100 years, evenly in log time
    assert grid[0] == 0.0 and len(grid) == 301
    assert grid[1] == pytest.approx(60 / 86_400) and grid[-1] == pytest.approx(36_500)
    assert grid[2] / grid[1] == pytest.approx(grid[-1] / grid[-2])


class _SavedCurveSources:
    """A collection backend holding three saved curve sources of one card."""

    def __init__(self) -> None:
        self.requests: list[tuple[int, str, int, int]] = []

    def get_rwkv_curve_sources(
        self, *, card_id: int, tag: scheduler_pb2.RwkvCurveSourceTag
    ) -> scheduler_pb2.RwkvCurveSources:
        self.requests.append((card_id, tag.model, tag.format, tag.kernel))
        return scheduler_pb2.RwkvCurveSources(
            revlog_ids=[1000, 5000, 9000], width=2, sources=b"aabbcc"
        )


class _RebuildingCurveBackend(_CardCurveBackend):
    def __init__(self, result: tuple[list[float], float] | None) -> None:
        super().__init__(result)
        self.rebuilt: list[rwkv_scheduler.RwkvCurveSources] = []

    def curve_source_tag(self) -> tuple[int, int]:
        return 7, 3

    def curves_from_sources(
        self,
        sources: rwkv_scheduler.RwkvCurveSources,
        elapsed_days: Sequence[float],
    ) -> list[tuple[list[float], float] | None]:
        self.rebuilt.append(sources)
        return [
            ([0.5] * len(elapsed_days), 1.5),
            None,
            ([0.25] * len(elapsed_days), 2.5),
        ]


def test_rwkv_card_info_rebuilds_the_saved_curve_of_each_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve: card info reads the card's
    sources saved under the running model's tag and has that model rebuild
    them; a source that rebuilds nothing gets no curve, and none stands in."""
    grid = rwkv_scheduler.RWKV_CARD_INFO_CURVE_DAYS
    backend = _RebuildingCurveBackend(([1.0] * len(grid), 3.25))
    _card_curve_ready(monkeypatch, backend)
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_model_cache_key", lambda: {"sha256": "abc"}
    )
    saved = _SavedCurveSources()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=saved)))

    curve = rwkv_scheduler.rwkv_card_info_curve(reviewer, SimpleNamespace(id=42))

    assert curve is not None
    assert saved.requests == [(42, "abc", 7, 3)]
    assert backend.rebuilt == [
        rwkv_scheduler.RwkvCurveSources(
            review_ids=[1000, 5000, 9000],
            sources=b"aabbcc",
            width=2,
            format=7,
            kernel=3,
        )
    ]
    assert [(past.review_id, past.s90) for past in curve.past] == [
        (1000, 1.5),
        (9000, 2.5),
    ]
    assert curve.past[0].recall == (0.5,) * len(grid)

    # without the model's identity no source can be matched to it: none is read
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_key", lambda: None)
    curve = rwkv_scheduler.rwkv_card_info_curve(reviewer, SimpleNamespace(id=42))
    assert curve is not None and curve.past == ()
    assert len(saved.requests) == 1


def test_rwkv_curve_source_writer_tags_every_source_with_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve: the saved sources carry the
    model's identity and their format and kernel; without the identity nothing
    is saved."""
    stored: list[scheduler_pb2.RwkvCurveSources] = []

    class ColBackend:
        def set_rwkv_curve_sources(
            self, message: scheduler_pb2.RwkvCurveSources
        ) -> None:
            stored.append(message)

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(_backend=ColBackend()))
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_model_cache_key", lambda: {"sha256": "abc"}
    )
    writer = rwkv_scheduler._RwkvCurveSourceWriter(reviewer)
    for review_ids, sources in (([11, 12], b"aabb"), ([13], b"cc")):
        writer(
            rwkv_scheduler.RwkvCurveSources(
                review_ids=review_ids, sources=sources, width=2, format=1, kernel=4
            )
        )
    writer.flush()
    assert len(stored) == 1
    assert (stored[0].tag.model, stored[0].tag.format, stored[0].tag.kernel) == (
        "abc",
        1,
        4,
    )
    assert list(stored[0].revlog_ids) == [11, 12, 13]
    assert (stored[0].width, stored[0].sources) == (2, b"aabbcc")
    assert writer.written == 3

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_key", lambda: None)
    writer = rwkv_scheduler._RwkvCurveSourceWriter(reviewer)
    writer(
        rwkv_scheduler.RwkvCurveSources(
            review_ids=[14], sources=b"dd", width=2, format=1, kernel=4
        )
    )
    writer.flush()
    assert len(stored) == 1


def test_rust_runtime_hands_each_chunks_curve_sources_with_review_ids() -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve: the replay records each
    answered review's source and hands it over under its review id; the
    recording stops when the replay ends."""
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.recording: list[bool] = []
            self.calls = 0

        def record_curve_sources(self, on: bool) -> None:
            self.recording.append(on)

        def warm_up_reviews_packed(
            self, reviews: bytes, record_predictions: bool
        ) -> list[tuple[int, float, float | None]]:
            self.calls += 1
            return []

        def take_curve_sources(self) -> tuple[bytes, bytes, int]:
            # the first review of each chunk only
            return struct.pack("<I", 0), bytes([self.calls, self.calls]), 2

        def curve_source_tag(self) -> tuple[int, int, int]:
            return 1, 1, 2

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    handed: list[rwkv_scheduler.RwkvCurveSources] = []

    runtime.warm_up_reviews(
        [
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
        ],
        review_ids=[101, 102],
        prediction_recorder=lambda review_id, retrievability: None,
        curve_source_recorder=handed.append,
        return_snapshot=False,
    )

    # on for each batch and off again after it, so the recording is never
    # left on while the runtime is free between two batches; off at the end
    assert process.recording == [True, False] * process.calls
    assert process.recording[-1] is False
    assert [(list(batch.review_ids), batch.sources) for batch in handed] == [
        ([101], b"\x01\x01"),
        ([102], b"\x02\x02"),
    ]
    assert {(batch.width, batch.format, batch.kernel) for batch in handed} == {
        (2, 1, 1)
    }


def test_live_answer_saves_its_curve_source_under_its_review_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve: a live answer's source is
    saved under the answer's own review, the card's newest answered one."""
    saved: list[rwkv_scheduler.RwkvCurveSources] = []

    class Writer:
        def __init__(self, reviewer: object) -> None:
            pass

        def __call__(self, sources: rwkv_scheduler.RwkvCurveSources) -> None:
            saved.append(sources)

        def flush(self) -> None:
            pass

    class Backend:
        def take_curve_sources(self) -> tuple[list[int], bytes, int]:
            return [0, 0], b"aabb", 2

        def curve_source_tag(self) -> tuple[int, int]:
            return 1, 1

    queries: list[tuple[str, tuple[object, ...]]] = []

    class Db:
        def scalar(self, sql: str, *args: object) -> int:
            queries.append((sql, args))
            return 777

    monkeypatch.setattr(rwkv_scheduler, "_RwkvCurveSourceWriter", Writer)
    sources = rwkv_scheduler._take_curve_sources(Backend())
    assert sources is not None
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=Db())))
    rwkv_scheduler._save_answered_curve_source(reviewer, 42, sources)

    # the last source recorded is the answer's own
    assert [(list(batch.review_ids), batch.sources) for batch in saved] == [
        ([777], b"bb")
    ]
    assert queries[0][1] == (42,)
    assert "ease > 0" in queries[0][0]


def test_rwkv_card_info_curve_gives_the_recall_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-one-algorithm"""
    grid = rwkv_scheduler.RWKV_CARD_INFO_CURVE_DAYS

    class Backend(_CardCurveBackend):
        def card_curve(
            self, card_id: int, elapsed_days: Sequence[float]
        ) -> tuple[list[float], float]:
            self.calls.append((card_id, tuple(elapsed_days)))
            return [1.0 - day / 100 for day in elapsed_days], 3.25

    backend = Backend(None)
    _card_curve_ready(monkeypatch, backend)

    curve = rwkv_scheduler.rwkv_card_info_curve(
        object(), SimpleNamespace(id=42), elapsed_days=2.5
    )

    assert curve is not None
    # one call: the drawn points, then the time since the last review
    assert backend.calls == [(42, (*grid, 2.5))]
    assert curve.elapsed_days == grid and len(curve.recall) == len(grid)
    assert curve.current_recall == pytest.approx(0.975)
    # without the elapsed time there is no recall now
    assert (
        rwkv_scheduler.rwkv_card_info_curve(
            object(), SimpleNamespace(id=42)
        ).current_recall
        is None
    )


@pytest.mark.parametrize("curve_preset", [True, False])
def test_rwkv_card_info_curve_is_none_without_a_curve(
    monkeypatch: pytest.MonkeyPatch, curve_preset: bool
) -> None:
    backend = _CardCurveBackend(None)
    _card_curve_ready(monkeypatch, backend, curve_preset=curve_preset)

    assert rwkv_scheduler.rwkv_card_info_curve(object(), SimpleNamespace(id=42)) is None
    # a card of another algorithm never asks RWKV
    assert bool(backend.calls) == curve_preset
    # RWKV answered: the card simply has no curve, so card info does not ask
    # again (spec ui.card-info-curve-messages)
    result = rwkv_scheduler.rwkv_card_info_curve_result(
        object(), SimpleNamespace(id=42)
    )
    assert result.curve is None and not result.pending


def test_rwkv_card_info_curve_result_is_pending_while_rwkv_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.card-info-curve-messages"""
    from contextlib import contextmanager

    backend = _CardCurveBackend((tuple([1.0]), 3.25))
    _card_curve_ready(monkeypatch, backend)

    # 1. the state is still loading
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_card_info",
        lambda reviewer: False,
    )
    result = rwkv_scheduler.rwkv_card_info_curve_result(
        object(), SimpleNamespace(id=42)
    )
    assert result.curve is None and result.pending

    # 2. the state is there but carries no token yet
    _card_curve_ready(monkeypatch, backend)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_capture_reviewer_backend_prediction_state_token",
        lambda reviewer, expected_backend: None,
    )
    result = rwkv_scheduler.rwkv_card_info_curve_result(
        object(), SimpleNamespace(id=42)
    )
    assert result.curve is None and result.pending

    # 3. another thread holds the state
    _card_curve_ready(monkeypatch, backend)

    @contextmanager
    def busy(**_kwargs: Any) -> Iterator[object]:
        yield None

    monkeypatch.setattr(rwkv_scheduler, "_try_reviewer_backend_prediction_access", busy)
    result = rwkv_scheduler.rwkv_card_info_curve_result(
        object(), SimpleNamespace(id=42)
    )
    assert result.curve is None and result.pending
    assert not backend.calls


def _stats_reuse_scaffold(
    days_elapsed: int = 42,
) -> tuple[Any, SimpleNamespace]:
    """A collection and an RWKV backend for the score-reuse tests
    (spec ui.stats-rwkv-scores-kept)."""

    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability=0.75)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    class Scheduler:
        def __init__(self) -> None:
            self.days_elapsed = days_elapsed

        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                days_elapsed=self.days_elapsed,
                next_day_at=(self.days_elapsed + 1) * 86_400,
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, "")]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            return [1]

    backend = Backend()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(_RwkvQueueScoreRpc())))
    return backend, reviewer


def test_prepare_stats_retrievability_scores_reuses_published_scores() -> None:
    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        first = prepare_stats_retrievability_scores(reviewer, "rated:7")
        second = prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert first == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    assert second == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    # the second request is the Stats page switching mode or period: it must
    # not score the search again
    assert backend.predicted_card_ids == [1]


def test_prepare_stats_retrievability_scores_scores_again_for_another_search() -> None:
    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
        prepare_stats_retrievability_scores(reviewer, "deck:current")
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert backend.predicted_card_ids == [1, 1, 1]


def test_prepare_stats_retrievability_scores_reuse_ends_with_the_time_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/scheduling.md#sched.rwkv-r-freshness: a kept map stands
    only as long as the shortest time tolerance of its cards. The scaffold's
    collection has no review, so its cards get the longest, an hour."""
    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    clock = [1_000_000.0]
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: clock[0])
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
        clock[0] += 30 * 60
        prepare_stats_retrievability_scores(reviewer, "rated:7")
        assert backend.predicted_card_ids == [1]
        clock[0] += 31 * 60
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert backend.predicted_card_ids == [1, 1]


def test_rwkv_scores_fresh_until_takes_the_shortest_tolerance_of_the_cards() -> None:
    """Pins spec/scheduling.md#sched.rwkv-r-freshness."""

    def inputs(*elapsed_seconds: int) -> list[tuple[int, Any]]:
        return [
            (
                index,
                replace(
                    _rwkv_review_input(card_id=index, note_id=index),
                    current_elapsed_seconds=elapsed,
                ),
            )
            for index, elapsed in enumerate(elapsed_seconds, start=1)
        ]

    fresh_until = rwkv_scheduler._rwkv_scores_fresh_until
    day = 86_400
    assert fresh_until(100.0, inputs(30 * day, 5 * 3600)) == 100.0 + 600
    assert fresh_until(100.0, inputs(30 * day, 30 * 60)) == 100.0 + 60
    # a card reviewed under ten minutes ago: the map is never reused
    assert fresh_until(100.0, inputs(30 * day, 5 * 60)) == 100.0
    assert fresh_until(100.0, inputs(30 * day, 3 * day)) == 100.0 + 3600


def test_prepare_stats_retrievability_scores_scores_again_after_a_new_day() -> None:
    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
        reviewer.mw.col.sched.days_elapsed += 1
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert backend.predicted_card_ids == [1, 1]


def _stats_curve_input_build(count: int) -> Any:
    """An input build of `count` review-ready query inputs."""

    inputs = [
        (
            card_id,
            replace(
                _rwkv_review_input(card_id=card_id, note_id=card_id * 10),
                current_elapsed_days=7,
            ),
        )
        for card_id in range(1, count + 1)
    ]
    return rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: inputs},
        loaded_rows=count,
        parsed_cards=count,
        cards_with_state=count,
        disabled_config_cards=0,
        eligible_cards=count,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )


def _patch_stats_search_scaffold(
    monkeypatch: pytest.MonkeyPatch,
    input_build: Any,
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_search",
        lambda **_kwargs: input_build,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_curve_enabled_input_build",
        lambda _reviewer, build: build,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_fresh_rwkv_review_queue_score_map",
        lambda _reviewer: {},
    )


def _patch_stored_curve_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    curve_retrievabilities: Callable[[int], list[float]],
    on_batch: Callable[[Sequence[tuple[int, RwkvReviewInput]]], None] | None = None,
) -> list[int]:
    """Stand in for the stored-curve lookup; returns the batch sizes the
    rating head was asked to score."""

    def curve_route(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        **_kwargs: Any,
    ) -> list[tuple[int, float]]:
        if on_batch is not None:
            on_batch(inputs_by_card_id)
        return list(
            zip(
                (card_id for card_id, _ in inputs_by_card_id),
                curve_retrievabilities(len(inputs_by_card_id)),
            )
        )

    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_stored_curve_retrievabilities_for_inputs", curve_route
    )
    rating_head_batches: list[int] = []

    def rating_head(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]], **_kwargs: Any
    ) -> list[tuple[int, float]]:
        rating_head_batches.append(len(inputs_by_card_id))
        return [(card_id, 0.7) for card_id, _ in inputs_by_card_id]

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_review_scores_for_inputs", rating_head)
    return rating_head_batches


def _rwkv_curve_by_formula(weights: Sequence[float], elapsed_seconds: float) -> float:
    """rslib's `predict_curve`, written out apart from the code under test:
    128 decays 0.9 ** (t / s), the scales spread by linspace_exp."""

    elapsed_seconds = max(elapsed_seconds, 1.0)
    raw = 0.0
    for index, weight in enumerate(weights):
        s_space = 0.1 + (math.exp(18.5 * index / 127) - 1.0) * math.exp(22.0 - 18.5)
        raw += weight * 0.9 ** (elapsed_seconds / s_space)
    return 1e-5 + (1.0 - 2e-5) * raw


def _packed_rwkv_curves(curves: Mapping[int, Sequence[float]]) -> bytes:
    return b"".join(
        struct.pack("<I", len(weights)) + struct.pack(f"<{len(weights)}f", *weights)
        for weights in curves.values()
    )


def test_rwkv_curve_r_is_the_stored_curve_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.rwkv-curve-r-stored-curve: a card's RWKV-Curve R is
    the curve stored at its last answered review, at the time since that
    review; RWKV's shared (deck, preset, global) state moving on after that
    review does not move it; a card with no stored curve gets no value, and
    no other value stands in for it."""

    build = _stats_curve_input_build(3)
    # card 1: 7 days; card 2: 90 seconds, so the seconds (not the days) are
    # the elapsed time; card 3: RWKV stored no curve for it
    inputs = build.inputs_by_batch_size[512]
    inputs[1] = (
        2,
        replace(inputs[1][1], current_elapsed_days=0, current_elapsed_seconds=90),
    )
    _patch_stats_search_scaffold(monkeypatch, build)
    # float32 weights, exactly as RWKV stores them
    stored = {
        1: list(array("f", [0.0] * 60 + [0.5] + [0.0] * 19 + [0.5] + [0.0] * 47)),
        2: list(array("f", [0.25] * 4 + [0.0] * 124)),
    }
    query_pass_value = [0.61]

    def query_pass(*_args: Any, **_kwargs: Any) -> Any:
        # a fresh query of each card reads the current deck, preset and
        # global states: the value must not come from it
        raise AssertionError("RWKV-Curve's R must not come from a query pass")

    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_review_predictions_for_inputs", query_pass
    )
    rating_head_calls: list[int] = []

    def rating_head(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]], **_kwargs: Any
    ) -> list[tuple[int, float]]:
        rating_head_calls.append(len(inputs_by_card_id))
        return [(card_id, query_pass_value[0]) for card_id, _ in inputs_by_card_id]

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_review_scores_for_inputs", rating_head)

    def card_curve_weights(card_ids: Sequence[int]) -> tuple[list[int], bytes]:
        held = {card_id: stored[card_id] for card_id in card_ids if card_id in stored}
        return list(held), _packed_rwkv_curves(held)

    backend = SimpleNamespace(
        cached_review_input_predictions=lambda _inputs: None,
        card_curve_weights=card_curve_weights,
    )
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        first = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="deck:current",
            prepare_curve_retrievability=True,
            prepare_instant_retrievability=False,
        )
        # other cards of the deck are answered: the shared states move on,
        # and a query pass would now give another value; cards 1 and 2 were
        # not answered, so their stored curves stay as they were
        query_pass_value[0] = 0.2
        second = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="deck:current",
            prepare_curve_retrievability=True,
            prepare_instant_retrievability=False,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert first is not None and second is not None
    expected = {
        1: _rwkv_curve_by_formula(stored[1], 7 * 86_400),
        2: _rwkv_curve_by_formula(stored[2], 90),
    }
    assert dict(first.curve_scores) == pytest.approx(expected, rel=1e-12)
    assert first.curve_scores == second.curve_scores
    # card 3 has no stored curve: no value at all, not a stand-in
    assert 3 not in dict(first.curve_scores)
    # nothing asked for RWKV-Instant's rating head, so it did not run
    assert rating_head_calls == []
    assert first.scores == []


def test_rwkv_curve_r_publishes_cards_without_the_rating_head() -> None:
    """The map carries a card RWKV-Curve has a value for even when the rating
    head did not score it; the rating-head field stays absent."""

    calls: list[dict[str, Any]] = []
    backend = SimpleNamespace(
        set_rwkv_stats_graph_scores=lambda **kwargs: calls.append(kwargs)
    )
    rwkv_scheduler._set_rwkv_stats_graph_scores(
        SimpleNamespace(),
        "deck:current",
        [(2, 0.7)],
        curve_retrievabilities_by_card_id={1: 0.8, 2: 0.6},
        collection_backend=backend,
    )

    (call,) = calls
    by_card = {score.card_id: score for score in call["scores"]}
    assert set(by_card) == {1, 2}
    assert not by_card[1].HasField("retrievability")
    assert by_card[1].curve_retrievability == pytest.approx(0.8)
    assert by_card[2].retrievability == pytest.approx(0.7)
    assert by_card[2].curve_retrievability == pytest.approx(0.6)


def test_rwkv_curve_r_request_runs_the_rating_head_only_for_its_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request for RWKV-Curve's R alone (the Stats graph, a Browser
    prop:rwkv-curve:r search) does not run RWKV-Instant's rating head; a
    request that also reads the head (prop:rwkv:r, a filtered deck ordered
    by retrievability) still gets it."""

    _patch_stats_search_scaffold(monkeypatch, _stats_curve_input_build(2))
    rating_head_batches = _patch_stored_curve_route(
        monkeypatch, curve_retrievabilities=lambda count: [0.6] * count
    )
    backend = SimpleNamespace(cached_review_input_predictions=lambda _inputs: None)
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        curve_only = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="deck:current",
            prepare_curve_retrievability=True,
            prepare_instant_retrievability=False,
        )
        assert rating_head_batches == []
        with_head = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="deck:current",
            prepare_curve_retrievability=True,
            prepare_instant_retrievability=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert curve_only is not None and with_head is not None
    assert curve_only.scores == []
    assert curve_only.curve_scores == [(1, 0.6), (2, 0.6)]
    assert rating_head_batches == [2]
    assert with_head.scores == [(1, 0.7), (2, 0.7)]
    assert with_head.curve_scores == [(1, 0.6), (2, 0.6)]


def test_prepare_stats_scores_asks_for_the_rating_head_only_when_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    def scorer(*, prepare_instant_retrievability: bool, **_kwargs: Any) -> None:
        seen.append(prepare_instant_retrievability)

    backend, reviewer = _stats_reuse_scaffold()
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_stats_graph_scores_for_search", scorer)
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_stats_graph_scores", lambda **_: [])
    previous_backend = set_reviewer_backend(backend)
    try:
        for search, curve in (
            ("deck:current", True),  # the Stats graph under RWKV-Curve
            ("prop:rwkv-curve:r<0.9", False),  # a Browser search
            ("prop:rwkv-curve:r<0.9 prop:rwkv:r>0.5", False),
            ("deck:current", False),  # RWKV-Instant's Stats graph
        ):
            rwkv_scheduler.forget_rwkv_stats_scores()
            prepare_stats_retrievability_scores(
                reviewer, search, prepare_curve_retrievability=curve
            )
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert seen == [False, False, True, True]


def test_stats_curve_due_keeps_the_full_prediction_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The curve-due flags need the current interval, so that request keeps
    the full path; the curve value still comes from the stored curve."""

    _patch_stats_search_scaffold(monkeypatch, _stats_curve_input_build(1))
    _patch_stored_curve_route(
        monkeypatch, curve_retrievabilities=lambda count: [0.55] * count
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_predictions_for_inputs",
        lambda *_args, **_kwargs: [
            RwkvReviewPrediction(
                retrievability=0.7,
                curve_retrievability=0.6,
                current_interval=5,
            )
        ],
    )

    backend = SimpleNamespace(cached_review_input_predictions=lambda _inputs: None)
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        result = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="is:rwkv-curve:due",
            prepare_curve_due=True,
            prepare_curve_retrievability=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert result is not None
    assert result.curve_due_card_ids == frozenset({1})
    assert result.curve_scores == [(1, 0.55)]


def test_backend_resident_curve_retrievability_requires_runtime_support() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    review_input = _rwkv_review_input(card_id=1, note_id=10)

    # no runtime support -> None, so the caller falls back to the full path
    assert not backend.supports_resident_curve_retrievability
    assert (
        backend.predict_curve_retrievability_inputs_from_warm_up([review_input]) is None
    )

    def predict_curve_retrievability_many_from_warm_up(
        review_inputs: list[RwkvReviewInput],
    ) -> list[float | None]:
        assert review_inputs == [review_input, review_input]
        return [0.42, None]

    runtime.predict_curve_retrievability_many_from_warm_up = (  # type: ignore[attr-defined]
        predict_curve_retrievability_many_from_warm_up
    )
    assert backend.supports_resident_curve_retrievability
    assert backend.predict_curve_retrievability_inputs_from_warm_up(
        [review_input, review_input]
    ) == [0.42, None]
    assert backend.predict_curve_retrievability_inputs_from_warm_up([]) == []


def test_rust_runtime_curve_retrievability_keeps_none() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    rows: list[tuple[object, ...]] = []

    def predict_curve_retrievability_many_from_warm_up(
        batch: list[tuple[object, ...]],
    ) -> list[float | None]:
        rows.extend(batch)
        return [0.42, None]

    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = SimpleNamespace(
        predict_curve_retrievability_many_from_warm_up=(
            predict_curve_retrievability_many_from_warm_up
        )
    )
    inputs = [
        _rwkv_review_input(card_id=1, note_id=10),
        _rwkv_review_input(card_id=2, note_id=20),
    ]

    outputs = runtime.predict_curve_retrievability_many_from_warm_up(inputs)

    assert len(rows) == 2 and rows[0][0] == 1 and rows[1][0] == 2
    assert outputs == [0.42, None]


def test_cancelled_stats_scoring_stops_at_the_next_batch_and_frees_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.stats-scoring-cancelled"""

    _patch_stats_search_scaffold(monkeypatch, _stats_curve_input_build(6))
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE", 2)
    scored_batches: list[int] = []

    def on_batch(inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]]) -> None:
        scored_batches.append(len(inputs_by_card_id))
        # the Stats window closes while the first batch runs
        rwkv_scheduler.cancel_stats_scoring()

    _patch_stored_curve_route(
        monkeypatch,
        curve_retrievabilities=lambda count: [0.6] * count,
        on_batch=on_batch,
    )

    backend = SimpleNamespace(cached_review_input_predictions=lambda _inputs: None)
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    generation = rwkv_scheduler._rwkv_stats_scoring_generation_now()
    try:
        with pytest.raises(rwkv_scheduler._RwkvStatsScoringCancelled):
            rwkv_scheduler._rwkv_stats_graph_scores_for_search(
                reviewer=SimpleNamespace(),
                search="deck:current",
                prepare_curve_retrievability=True,
                cancel_generation=generation,
            )
    finally:
        set_reviewer_backend(previous_backend)

    # it stopped at the boundary after the first batch, not after all three
    assert scored_batches == [2]
    # the boundary is outside the RWKV lock, so the lock is free again
    assert rwkv_scheduler._reviewer_backend_execution_lock.acquire(blocking=False)
    rwkv_scheduler._reviewer_backend_execution_lock.release()


def test_stats_scoring_without_a_cancel_generation_is_never_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Browser search or a filtered deck asks for the same scores; closing
    the Stats window must not stop their work."""

    _patch_stats_search_scaffold(monkeypatch, _stats_curve_input_build(4))
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE", 2)
    scored_batches: list[int] = []

    def on_batch(inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]]) -> None:
        scored_batches.append(len(inputs_by_card_id))
        rwkv_scheduler.cancel_stats_scoring()

    _patch_stored_curve_route(
        monkeypatch,
        curve_retrievabilities=lambda count: [0.6] * count,
        on_batch=on_batch,
    )

    backend = SimpleNamespace(cached_review_input_predictions=lambda _inputs: None)
    previous_backend = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        result = rwkv_scheduler._rwkv_stats_graph_scores_for_search(
            reviewer=SimpleNamespace(),
            search="deck:current",
            prepare_curve_retrievability=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scored_batches == [2, 2]
    assert result is not None and len(result.curve_scores) == 4


def _cancelling_stats_scorer(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Make the search scoring stop the way a closed Stats window stops it."""

    seen: list[int | None] = []

    def scorer(*, cancel_generation: int | None = None, **_kwargs: Any) -> None:
        seen.append(cancel_generation)
        rwkv_scheduler.cancel_stats_scoring()
        rwkv_scheduler._raise_if_stats_scoring_cancelled(cancel_generation)
        raise AssertionError("the scoring should have stopped")

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_stats_graph_scores_for_search", scorer)
    return seen


def test_prepare_stats_retrievability_scores_stop_when_the_stats_window_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.stats-scoring-cancelled"""

    backend, reviewer = _stats_reuse_scaffold()
    seen = _cancelling_stats_scorer(monkeypatch)
    published: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_set_rwkv_stats_graph_scores",
        lambda *args, **kwargs: published.append(args),
    )
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        status = prepare_stats_retrievability_scores(
            reviewer,
            "deck:current",
            cancel_when_stats_closes=True,
        )
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert len(seen) == 1 and seen[0] is not None
    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.FAILED
    # no half-published map, and the fallback scoring never ran either
    assert published == []
    assert backend.predicted_card_ids == []


def test_cancelled_stats_scoring_drops_the_kept_score_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.stats-scoring-cancelled"""

    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
        assert backend.predicted_card_ids == [1]
        with monkeypatch.context() as cancelled:
            _cancelling_stats_scorer(cancelled)
            assert (
                prepare_stats_retrievability_scores(
                    reviewer,
                    "deck:current",
                    cancel_when_stats_closes=True,
                )
                == rwkv_scheduler.RwkvStatsPreparationStatus.FAILED
            )
        # the memo is gone, so the next request scores again instead of
        # reusing a map the cancelled job never published
        assert (
            prepare_stats_retrievability_scores(reviewer, "rated:7")
            == rwkv_scheduler.RwkvStatsPreparationStatus.READY
        )
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert backend.predicted_card_ids == [1, 1]


def test_stats_scoring_after_a_cancel_runs_to_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.stats-scoring-cancelled"""

    backend, reviewer = _stats_reuse_scaffold()
    previous_backend = set_reviewer_backend(backend)
    rwkv_scheduler.forget_rwkv_stats_scores()
    try:
        with monkeypatch.context() as cancelled:
            _cancelling_stats_scorer(cancelled)
            prepare_stats_retrievability_scores(
                reviewer,
                "deck:current",
                cancel_when_stats_closes=True,
            )
        # the window opens again: the new request starts from the new
        # generation, so the earlier cancel does not stop it
        status = prepare_stats_retrievability_scores(
            reviewer,
            "deck:current",
            cancel_when_stats_closes=True,
        )
    finally:
        set_reviewer_backend(previous_backend)
        rwkv_scheduler.forget_rwkv_stats_scores()

    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.READY
    assert backend.predicted_card_ids == [1]


class _StartupTaskman:
    """Records which of the two paths the start-up work took: a progress
    window, or the background with no window at all."""

    def __init__(self) -> None:
        self.windows: list[dict[str, object]] = []
        self.background_runs = 0
        self.background_uses_collection: list[bool] = []

    def run_on_main(self, callback: Callable[[], None]) -> None:
        callback()

    def with_progress(
        self,
        task: Callable[[], object],
        on_done: Callable[[Future[object]], None],
        **kwargs: object,
    ) -> None:
        self.windows.append(kwargs)
        self._run(task, on_done)

    def run_in_background(
        self,
        task: Callable[[], object],
        on_done: Callable[[Future[object]], None],
        *,
        uses_collection: bool = True,
    ) -> None:
        self.background_runs += 1
        self.background_uses_collection.append(uses_collection)
        self._run(task, on_done)

    def _run(
        self,
        task: Callable[[], object],
        on_done: Callable[[Future[object]], None],
    ) -> None:
        future: Future[object] = Future()
        try:
            future.set_result(task())
        except Exception as exc:
            future.set_exception(exc)
        on_done(future)


# Pins spec/scheduling.md#sched.rwkv-startup-no-window: opening a profile
# opens no progress window and leaves the main window alone.
def test_the_startup_restore_opens_no_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    taskman = _StartupTaskman()
    progress_updates: list[object] = []
    profile = tmp_path / "converted"
    _state_store_at_schema(
        profile, rwkv_scheduler._RWKV_STATE_CACHE_STORE_SCHEMA_VERSION
    )
    mw = SimpleNamespace(
        taskman=taskman,
        progress=SimpleNamespace(
            update=lambda **kwargs: progress_updates.append(kwargs)
        ),
        pm=SimpleNamespace(profileFolder=lambda: str(profile)),
    )
    reported: list[object] = []

    def load(_mw: object, *, progress: object = None) -> bool:
        reported.append(progress)
        return True

    monkeypatch.setattr(rwkv_scheduler, "load_rwkv_state_cache", load)

    rwkv_scheduler.load_rwkv_state_cache_with_progress(mw)

    assert taskman.windows == []
    assert taskman.background_runs == 1
    # and off the one collection worker, so a click does not queue behind the
    # restore (report B-011); the collection-use count is kept by hand instead
    assert taskman.background_uses_collection == [False]
    # nothing reports to a window that is not there
    assert reported == [None]
    assert progress_updates == []
    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False


# Pins spec/scheduling.md#sched.rwkv-startup-no-window and
# #sched.rwkv-state-cache-startup-build: the build start-up runs by itself
# gets no window either, while a build a button starts keeps one.
def test_the_startup_build_opens_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a quiet build runs in the background; a build a button starts still
    # opens its window
    taskman = _StartupTaskman()
    mw = SimpleNamespace(taskman=taskman)
    monkeypatch.setattr(
        rwkv_scheduler,
        "warm_up_rwkv_state",
        lambda _mw, **_kwargs: False,
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_finish_rwkv_state_cache_operation",
        lambda _mw, **_kwargs: None,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_tr",
        lambda: SimpleNamespace(
            qt_misc_review_history_reading=lambda: "Reading",
            qt_misc_review_history_title=lambda: "Getting Ready",
            qt_misc_review_history_failed=lambda: "failed",
            qt_misc_review_history_ready=lambda: "ready",
        ),
    )

    rwkv_scheduler.build_rwkv_state_cache_with_progress(mw, quiet=True)
    assert (taskman.windows, taskman.background_runs) == ([], 1)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(mw)
    assert (len(taskman.windows), taskman.background_runs) == (1, 1)

    # and start-up asks for the quiet one
    quiet_flags: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "build_rwkv_state_cache_with_progress",
        lambda _mw, **kwargs: quiet_flags.append(kwargs.get("quiet")),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "configure_reviewer_backend_from_environment",
        lambda: True,
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_resident_state_ready", lambda _mw: False)
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_startup_build_started", False)

    rwkv_scheduler._start_rwkv_state_cache_build(mw)

    assert quiet_flags == [True]


# Pins spec/scheduling.md#sched.rwkv-startup-no-window: card info is built
# once when it opens, so its "being computed" message would stay until the
# user picked another card.
def test_open_card_info_is_drawn_again_when_the_rwkv_state_is_ready() -> None:
    from aqt.browser.card_info import CardInfoDialog

    class _OpenCardInfo(CardInfoDialog):
        def __init__(self) -> None:  # no Qt dialog, no webview
            self.redraws = 0
            self.web = object()
            self._card_id = 7

        def isVisible(self) -> bool:
            return True

        def redraw(self) -> None:
            self.redraws += 1

    class _ClosedCardInfo(_OpenCardInfo):
        def isVisible(self) -> bool:
            return False

    opened = _OpenCardInfo()
    closed = _ClosedCardInfo()
    other = SimpleNamespace(isVisible=lambda: True)
    mw = SimpleNamespace(
        app=SimpleNamespace(topLevelWidgets=lambda: [other, opened, closed])
    )

    rwkv_scheduler._redraw_open_card_info(mw)

    assert (opened.redraws, closed.redraws) == (1, 0)


def _state_store_at_schema(profile_folder: Path, version: int) -> None:
    cache_dir = profile_folder / rwkv_scheduler._RWKV_STATE_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / rwkv_scheduler._RWKV_STATE_CACHE_STORE_FILE
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"pragma user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def _startup_window(
    monkeypatch: pytest.MonkeyPatch,
    profile_folder: Path,
) -> dict[str, object] | None:
    """The kwargs of the progress window the start-up opened, or None where
    it opened none."""
    taskman = _StartupTaskman()
    mw = SimpleNamespace(
        taskman=taskman,
        progress=SimpleNamespace(update=lambda **_kwargs: None),
        pm=SimpleNamespace(profileFolder=lambda: str(profile_folder)),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache",
        lambda _mw, *, progress=None: True,
    )
    rwkv_scheduler.load_rwkv_state_cache_with_progress(mw)
    assert len(taskman.windows) + taskman.background_runs == 1
    return taskman.windows[0] if taskman.windows else None


# Pins spec/scheduling.md#sched.rwkv-lazy-state-upgrade-window
def test_only_the_one_time_upgrade_gets_the_one_time_words(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aqt.utils import tr

    # a store in the old format: the start-up converts it once, and says so
    old = tmp_path / "old"
    _state_store_at_schema(old, 4)
    assert rwkv_scheduler.rwkv_state_cache_store_needs_upgrade(
        SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(old)))
    )
    window = _startup_window(monkeypatch, old)
    assert window is not None
    assert window["title"] == tr.qt_misc_rwkv_state_upgrade_title()
    assert "reorganising" in cast(str, window["label"])
    assert window["label"] == tr.qt_misc_rwkv_state_upgrade_label()

    # a store already converted: no window at all (spec
    # sched.rwkv-startup-no-window). The two paths shared one string until
    # the lazy load arrived, so a later change could give the ordinary
    # start-up a window again without anyone noticing.
    converted = tmp_path / "new"
    _state_store_at_schema(converted, 5)
    assert not rwkv_scheduler.rwkv_state_cache_store_needs_upgrade(
        SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(converted)))
    )
    assert _startup_window(monkeypatch, converted) is None

    # and a profile with no store at all is not an upgrade either
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not rwkv_scheduler.rwkv_state_cache_store_needs_upgrade(
        SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(empty)))
    )


class _CurveCacheRuntime(_CacheRuntime):
    """A runtime that reports RWKV-Curve's value for a past review.

    Its query answers carry `curve_retrievability`, and a card's FIRST query
    carries none, as the model has no stored curve before a card's first
    answer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.queried_cards: list[int] = []

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        if review_input.ease is None:
            card_id = review_input.identity.card_id
            seen = card_id in self.queried_cards
            self.queried_cards.append(card_id)
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(
                    retrievability=0.45,
                    curve_retrievability=0.62 if seen else None,
                )
            )
        return super().review(
            review_input=review_input,
            card_state=card_state,
            note_state=note_state,
            deck_state=deck_state,
            preset_state=preset_state,
            global_state=global_state,
        )


def test_rwkv_calibration_recompute_records_the_curve_of_every_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    backend = RwkvStatefulReviewerBackend(_CurveCacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True

    reviewer.mw.col.review_prediction_rows.clear()
    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    # the card's first review has no stored curve before it, so it gets no
    # row; the second review gets RWKV-Curve's own value, under RWKV-Curve's
    # own algorithm id (spec ui.stats-model-metrics)
    assert reviewer.mw.col.review_prediction_rows == [
        (
            int(rwkv_scheduler._RWKV_CURVE_ALGORITHM),
            second_review,
            pytest.approx(0.62),
            "rwkv_curve_calibration_recompute",
            rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
        )
    ]


# Pins spec/ui.md#ui.stats-model-metrics: building the RWKV state cache
# writes RWKV-Curve's per-review rows beside RWKV-Instant's, so a collection
# whose state was built has both series without a separate pass.
def test_the_state_cache_build_records_rwkv_curve_rows_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    set_reviewer_backend(RwkvStatefulReviewerBackend(_CurveCacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert (
        rwkv_scheduler.warm_up_rwkv_state(reviewer.mw, record_retrievability_cache=True)
        is True
    )

    assert reviewer.mw.col.review_prediction_rows == [
        (
            int(rwkv_scheduler._RWKV_CURVE_ALGORITHM),
            second_review,
            pytest.approx(0.62),
            "rwkv_curve_state_cache_build",
            rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
        )
    ]
    assert [row[0] for row in reviewer.mw.col.rwkv_retrievability_rows] == [
        first_review,
        second_review,
    ]


class _TaggedCurveCacheRuntime(_CurveCacheRuntime):
    def curve_source_tag(self) -> tuple[int, int]:
        return 1, 1


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a full recording
# pass remembers what it recorded with, so the next start-up finds the
# recordings current and runs no pass.
def test_a_full_recording_pass_marks_the_recordings_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        ((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500),
        ((41 * 86_400 + 3_700) * 1000, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test", "sha256": "abc"},
    )
    set_reviewer_backend(RwkvStatefulReviewerBackend(_TaggedCurveCacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    marker = tmp_path / "rwkv-state-cache" / "recordings.json"
    assert not marker.exists()

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    saved = json.loads(marker.read_text(encoding="utf-8"))
    tag = rwkv_scheduler._rwkv_recordings_tag(reviewer.mw)
    assert tag is not None
    assert all(saved[key] == value for key, value in tag.items())
    assert (saved["model"], saved["format"], saved["kernel"]) == ("abc", 1, 1)
    # and how much it recorded, so that "the rows are still there" is a count
    # rather than the question whether one row exists (spec
    # sched.rwkv-recordings-automatic)
    assert (saved["reviews"], saved["lastReviewId"]) == (len(rows), rows[-1][0])


class _BulkRecordingRuntime(_TaggedCurveCacheRuntime):
    """A runtime whose bulk replay reports all three per-review recordings:
    RWKV-Instant's value, RWKV-Curve's value and the curve source."""

    def warm_up_reviews(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        curve_recorder: Callable[[int, float], None] | None = None,
        curve_source_recorder: Callable[[RwkvCurveSources], None] | None = None,
        progress: object = None,
        return_snapshot: bool = True,
    ) -> RwkvBackendCacheSnapshot:
        del progress, return_snapshot
        seen: set[int] = set()
        states: dict[int, bytes] = {}
        answered_ids: list[int] = []
        for index, review_input in enumerate(reviews):
            if review_input.ease is None or review_ids is None:
                continue
            card_id = review_input.identity.card_id
            review_id = review_ids[index]
            if prediction_recorder is not None:
                prediction_recorder(review_id, 0.45)
            if curve_recorder is not None and card_id in seen:
                curve_recorder(review_id, 0.62)
            seen.add(card_id)
            answered_ids.append(review_id)
            self.reviewed.append((card_id, review_input.ease))
            states[card_id] = f"card-{card_id}".encode()
        if curve_source_recorder is not None and answered_ids:
            curve_source_recorder(
                RwkvCurveSources(
                    review_ids=answered_ids,
                    sources=b"ab" * len(answered_ids),
                    width=2,
                    format=1,
                    kernel=1,
                )
            )
        return RwkvBackendCacheSnapshot(
            card_states=states,
            note_states={},
            deck_states={},
            preset_states={},
            global_state=None,
            runtime_state=None,
        )


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a state-cache
# build that replays the whole history with every recorder is the
# recording pass too, so no automatic pass is due right after it.
def test_a_full_recording_build_leaves_no_recording_pass_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        ((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500),
        ((41 * 86_400 + 3_700) * 1000, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test", "sha256": "abc"},
    )
    set_reviewer_backend(RwkvStatefulReviewerBackend(_BulkRecordingRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    saved_sources: list[scheduler_pb2.RwkvCurveSources] = []
    reviewer.mw.col._backend.set_rwkv_curve_sources = saved_sources.append
    marker = tmp_path / "rwkv-state-cache" / "recordings.json"

    # a build that records nothing per review is not the recording pass
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    assert not marker.exists()

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw, force_rebuild=True, record_retrievability_cache=True
        )
        is True
    )
    recorded_with = json.loads(marker.read_text(encoding="utf-8"))
    build_tag = rwkv_scheduler._rwkv_recordings_tag(reviewer.mw)
    assert build_tag is not None
    assert all(recorded_with[key] == value for key, value in build_tag.items())
    assert recorded_with["reviews"] == len(rows)
    col = reviewer.mw.col
    assert col.rwkv_retrievability_rows and col.review_prediction_rows
    assert [list(batch.revlog_ids) for batch in saved_sources][-1] == [
        row[0] for row in rows
    ]
    passes: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler, "recompute_rwkv_calibration_data_in_background", passes.append
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_recordings_pass_started", False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda reviewer: SimpleNamespace(review_enabled=True),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "rwkv_recordings_current", lambda mw: marker.exists()
    )
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(reviewer.mw)
    assert passes == []


def _recordings_mw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, rows_present: bool = True
) -> tuple[SimpleNamespace, list[object]]:
    """A profile whose RWKV state is ready, with the recording pass watched."""
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: True
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_model_cache_key", lambda: {"sha256": "abc"}
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(curve_source_tag=lambda: (1, 1)),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_replay_semantics_key", lambda reviewer, **_: "replay"
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda reviewer: SimpleNamespace(review_enabled=True),
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_recordings_pass_started", False)
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_recordings_known_current", False)
    passes: list[object] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "recompute_rwkv_calibration_data_in_background",
        passes.append,
    )
    # the idle wait's timer, fired by the test with mw.fire_timer()
    timers: list[Callable[[], None]] = []

    def timer(
        ms: int, fn: Callable[[], None], repeat: bool, **kwargs: object
    ) -> object:
        timers.append(fn)
        return SimpleNamespace()

    counted = (1, 1, 1) if rows_present else (0, 0, 0)
    mw = SimpleNamespace(
        col=SimpleNamespace(
            db=SimpleNamespace(
                scalar=lambda sql, *args: int(rows_present),
                all=lambda sql, *args: [counted],
            )
        ),
        pm=SimpleNamespace(profileFolder=lambda: str(tmp_path)),
        state="deckBrowser",
        progress=SimpleNamespace(timer=timer),
        app=SimpleNamespace(last_input_at=time.monotonic() - 600),
        fire_timer=lambda: timers.pop()(),
        timers=timers,
    )
    return mw, passes


def _recordings_marker(mw: object, **fields: object) -> None:
    """Writes the marker a finished pass leaves, with `fields` overriding it."""
    tag = rwkv_scheduler._rwkv_recordings_tag(mw)
    assert tag is not None
    rows = fields.get("rows", (1, 1, 1))
    assert isinstance(rows, tuple)
    rwkv_scheduler._write_rwkv_recordings_marker(
        mw,
        dict(
            tag,
            **{
                key: value
                for key, value in fields.items()
                if key in tag and value is not None
            },
        ),
        reviews=int(fields.get("reviews", 1)),  # type: ignore[arg-type]
        last_review_id=int(fields.get("last_review_id", 1)),  # type: ignore[arg-type]
        rows=rows,
    )


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a screen that
# reads the rows starts the pass, and the rows decide whether it is needed.
@pytest.mark.parametrize(
    "case", ["never_recorded", "other_model", "rows_gone", "current"]
)
def test_a_screen_that_reads_the_rows_starts_the_recording_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    mw, passes = _recordings_mw(monkeypatch, tmp_path, rows_present=case != "rows_gone")
    if case != "never_recorded":
        _recordings_marker(mw, model="old" if case == "other_model" else None)

    expected: list[object] = [] if case == "current" else [mw]
    counts: list[object] = []
    inner = rwkv_scheduler._rwkv_recorded_row_counts
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_recorded_row_counts",
        lambda *args: counts.append(args) or inner(*args),
    )
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == expected
    # and only one pass at a time, however often a screen asks
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == expected
    # Counting the rows takes about 200 ms on a large cache, so it happens
    # at most once: a marker that is missing or another model's is answered
    # without counting at all, and an answer of "they are the running
    # model's" is remembered rather than asked for again.
    assert len(counts) == (1 if case in ("current", "rows_gone") else 0)


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: nothing starts the
# pass at profile open. Start-up is never the moment for minutes of work, and
# nothing about reviewing needs these rows.
def test_nothing_starts_the_recording_pass_at_profile_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mw, passes = _recordings_mw(monkeypatch, tmp_path, rows_present=False)
    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache_metadata", lambda reviewer: None
    )

    rwkv_scheduler.start_rwkv_maintenance_if_needed(mw)
    assert passes == []
    # not on a timer either: there is no timer to fire
    assert mw.timers == []

    # the Stats page is what starts it
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == [mw]


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the rows of one
# review do not stand for the whole history. Andrew's collection held 38
# curve sources, every one written by an answer in the reviewer, next to
# 656,402 reviews.
def test_rows_of_one_review_do_not_make_the_recordings_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[str] = []

    def all_rows(sql: str, *args: object) -> list[tuple[int, int, int]]:
        asked.append(sql)
        # one row of each kind is on disk, as one answered card leaves
        return [(1, 1, 1)]

    mw, passes = _recordings_mw(monkeypatch, tmp_path)
    mw.col.db.all = all_rows
    # what a finished pass over 656,402 reviews would have written
    _recordings_marker(
        mw, reviews=656_402, last_review_id=99, rows=(656_402, 600_000, 656_402)
    )

    # 1 row of each kind is not the whole history
    assert rwkv_scheduler.rwkv_recordings_current(mw) is False
    assert asked and "count(" in asked[0], (
        "the test counts rows rather than asking whether one exists"
    )

    # and a pass that really did record one review of each kind is current
    _recordings_marker(mw, reviews=1, last_review_id=99, rows=(1, 1, 1))
    assert rwkv_scheduler.rwkv_recordings_current(mw) is True
    del passes


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic
def test_the_recording_pass_waits_until_no_card_is_being_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mw, passes = _recordings_mw(monkeypatch, tmp_path, rows_present=False)
    mw.state = "review"

    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == []

    # the card is answered and the review screen closes
    mw.state = "deckBrowser"
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == [mw]


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic
def test_nothing_starts_while_the_main_window_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mw, passes = _recordings_mw(monkeypatch, tmp_path, rows_present=False)
    # a sync, or the profile closing
    mw.isEnabled = lambda: False
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == []
    mw.isEnabled = lambda: True
    rwkv_scheduler.start_rwkv_recordings_pass_if_needed(mw)
    assert passes == [mw]


def test_rwkv_calibration_recompute_refuses_a_backend_that_cannot_record_the_curve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    class _BackendWithoutCurveRecorder:
        """A backend whose warm-up predates RWKV-Curve's recorder."""

        def __init__(self) -> None:
            self.replays: list[int] = []

        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            return RwkvBackendCacheSnapshot(
                card_states={},
                note_states={},
                deck_states={},
                preset_states={},
                global_state=None,
                runtime_state=None,
            )

        def restore_cache_snapshot(
            self, snapshot: RwkvBackendCacheSnapshot
        ) -> None: ...

        def reset_cache_snapshot(self) -> None: ...

        def warm_up(
            self,
            reviews: Sequence[RwkvReviewInput],
            *,
            review_ids: Sequence[int] | None = None,
            prediction_recorder: Any = None,
            progress: Any = None,
            snapshot_after_reviews: Sequence[int] = (),
            snapshot_recorder: Any = None,
        ) -> None:
            self.replays.append(len(reviews))

    backend = _BackendWithoutCurveRecorder()
    set_reviewer_backend(cast(Any, backend))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    with pytest.raises(rwkv_scheduler.RwkvCurveRecordingUnavailable):
        rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw)

    # it refuses BEFORE the replay: the old guard skipped the recorder and
    # then walked the whole history for minutes, writing nothing
    assert backend.replays == []
    assert reviewer.mw.col.review_prediction_rows == []


def test_bulk_warm_up_is_handed_the_curve_recorder() -> None:
    recorded: list[tuple[int, float]] = []
    curve_recorded: list[tuple[int, float]] = []

    class _BulkCurveRuntime:
        def __init__(self) -> None:
            self.curve_recorders: list[Any] = []

        def warm_up_reviews(
            self,
            reviews: Sequence[RwkvReviewInput],
            *,
            review_ids: Sequence[int] | None,
            prediction_recorder: Any,
            curve_recorder: Any = None,
            progress: Any = None,
        ) -> RwkvBackendCacheSnapshot:
            self.curve_recorders.append(curve_recorder)
            assert review_ids is not None
            prediction_recorder(review_ids[0], 0.31)
            if curve_recorder is not None:
                curve_recorder(review_ids[0], 0.57)
            return RwkvBackendCacheSnapshot(
                card_states={1: b"card-1"},
                note_states={10: b"note-10"},
                deck_states={100: b"deck-100"},
                preset_states={1000: b"preset-1000"},
                global_state=b"global",
                runtime_state=b"runtime",
            )

        def review(self, **kwargs: object) -> RwkvReviewTransition:
            raise AssertionError("bulk warm-up should replace per-review replay")

    runtime = _BulkCurveRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    backend.warm_up(
        [_rwkv_review_input(card_id=1, note_id=10)],
        review_ids=[41],
        prediction_recorder=lambda review_id, value: recorded.append(
            (review_id, value)
        ),
        curve_recorder=lambda review_id, value: curve_recorded.append(
            (review_id, value)
        ),
    )

    assert recorded == [(41, 0.31)]
    assert curve_recorded == [(41, 0.57)]
    assert runtime.curve_recorders and runtime.curve_recorders[0] is not None


def test_bulk_warm_up_without_a_curve_recorder_keyword_says_so() -> None:
    class _BulkRuntimeWithoutCurve:
        def __init__(self) -> None:
            self.replays = 0

        def warm_up_reviews(
            self,
            reviews: Sequence[RwkvReviewInput],
            *,
            review_ids: Sequence[int] | None,
            prediction_recorder: Any,
            progress: Any = None,
        ) -> RwkvBackendCacheSnapshot:
            self.replays += 1
            raise AssertionError("the bulk warm-up must not run")

        def review(self, **kwargs: object) -> RwkvReviewTransition:
            raise AssertionError("bulk warm-up should replace per-review replay")

    runtime = _BulkRuntimeWithoutCurve()
    backend = RwkvStatefulReviewerBackend(runtime)

    with pytest.raises(rwkv_scheduler.RwkvCurveRecordingUnavailable):
        backend.warm_up(
            [_rwkv_review_input(card_id=1, note_id=10)],
            review_ids=[41],
            prediction_recorder=lambda review_id, value: None,
            curve_recorder=lambda review_id, value: None,
        )

    assert runtime.replays == 0


def test_a_query_with_no_prediction_is_skipped_not_reported() -> None:
    """A runtime with nothing to say about a review is not a runtime that
    cannot report curves. RWKV-Instant's recorder skips such a row, so
    RWKV-Curve's does too, and the loud error stays for the real case: a
    prediction that has no curve field at all."""

    curve_recorded: list[tuple[int, float]] = []

    class _SilentQueryRuntime(_CacheRuntime):
        def review(
            self,
            *,
            review_input: RwkvReviewInput,
            card_state: object | None,
            note_state: object | None,
            deck_state: object | None,
            preset_state: object | None,
            global_state: object | None,
        ) -> RwkvReviewTransition:
            if review_input.ease is None:
                return RwkvReviewTransition()
            return super().review(
                review_input=review_input,
                card_state=card_state,
                note_state=note_state,
                deck_state=deck_state,
                preset_state=preset_state,
                global_state=global_state,
            )

    backend = RwkvStatefulReviewerBackend(_SilentQueryRuntime())
    backend.warm_up(
        [_rwkv_answered_review_input(card_id=1, note_id=10, ease=3)],
        review_ids=[41],
        prediction_recorder=lambda review_id, value: None,
        curve_recorder=lambda review_id, value: curve_recorded.append(
            (review_id, value)
        ),
    )
    assert curve_recorded == []

    # but a prediction object with no curve field at all still says so
    class _NoCurveFieldRuntime(_CacheRuntime):
        def review(
            self,
            *,
            review_input: RwkvReviewInput,
            card_state: object | None,
            note_state: object | None,
            deck_state: object | None,
            preset_state: object | None,
            global_state: object | None,
        ) -> RwkvReviewTransition:
            if review_input.ease is None:
                return RwkvReviewTransition(
                    prediction=cast(Any, SimpleNamespace(retrievability=0.45))
                )
            return super().review(
                review_input=review_input,
                card_state=card_state,
                note_state=note_state,
                deck_state=deck_state,
                preset_state=preset_state,
                global_state=global_state,
            )

    backend = RwkvStatefulReviewerBackend(_NoCurveFieldRuntime())
    with pytest.raises(rwkv_scheduler.RwkvCurveRecordingUnavailable):
        backend.warm_up(
            [_rwkv_answered_review_input(card_id=1, note_id=10, ease=3)],
            review_ids=[41],
            prediction_recorder=lambda review_id, value: None,
            curve_recorder=lambda review_id, value: None,
        )


def _rwkv_answered_review_input(
    *, card_id: int, note_id: int, ease: int
) -> RwkvReviewInput:
    return replace(
        _rwkv_review_input(card_id=card_id, note_id=note_id),
        is_query=False,
        ease=ease,
        duration_millis=1234,
    )


def _plain(label: object) -> str:
    """A progress label without Fluent's invisible direction marks."""
    from anki.lang import without_unicode_isolation

    return without_unicode_isolation(str(label))


def _tr() -> Any:
    from aqt.utils import tr

    return tr


# Pins spec/scheduling.md#sched.filtered-deck-one-algorithm
def test_filtered_deck_prepares_rwkv_scores_for_relative_overdueness() -> None:
    # under RWKV the order reads the deck's own RWKV scores, so the build
    # must prepare them for it as for the retrievability orders
    assert (
        FilteredDeckConfig.SearchTerm.RELATIVE_OVERDUENESS
        in rwkv_scheduler._FILTERED_DECK_RETRIEVABILITY_ORDERS
    )


# Pins spec/scheduling.md#sched.rwkv-recordings-progress: a stopped pass goes
# on where it stopped. Measured on Andrew's 656,402 reviews, one snapshot of
# the RWKV state is 3.3 GB, so the pass cannot store where it was; it stores
# how far its rows reach and replays the prefix again with nothing recorded.
def test_a_stopped_pass_goes_on_where_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mw = _resume_mw(monkeypatch, tmp_path)
    review_ids = [100, 200, 300, 400, 500]
    _write_resume_point(mw, reviews=3, last_review_id=300, rows=(3, 2, 3))

    point = rwkv_scheduler.rwkv_recordings_resume_point(mw, review_ids)
    assert point is not None
    assert (point.reviews, point.rows) == (3, (3, 2, 3))


# Pins spec/scheduling.md#sched.rwkv-recordings-progress: a pass writes its
# first step before it reads the resume point, so that write must carry the
# point forward. Dropping it destroyed the work the pass was about to carry
# on from, and every run then started over.
def test_a_starting_pass_keeps_the_resume_point_it_has_not_read_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mw = _resume_mw(monkeypatch, tmp_path)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_collection_config_state",
        lambda reviewer: SimpleNamespace(review_enabled=True),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "recompute_rwkv_calibration_data", lambda mw, **kwargs: False
    )
    monkeypatch.setattr(rwkv_scheduler, "_run_on_main", lambda mw, fn: fn())
    _write_resume_point(mw, reviews=3, last_review_id=300, rows=(3, 2, 3))

    rwkv_scheduler.recompute_rwkv_calibration_data_in_background(mw)
    for _ in range(200):
        if not rwkv_scheduler.rwkv_recordings_pass_running():
            break
        time.sleep(0.01)

    kept = rwkv_scheduler.rwkv_recordings_progress(mw)
    assert kept is not None
    assert (kept["reviews"], kept["lastReviewId"]) == (3, 300)
    assert rwkv_scheduler.rwkv_recordings_resume_point(mw, [100, 200, 300, 400]) == (
        rwkv_scheduler.RwkvRecordingsResumePoint(reviews=3, rows=(3, 2, 3))
    )


# Pins spec/scheduling.md#sched.rwkv-recordings-progress: correctness first.
@pytest.mark.parametrize(
    "case", ["other_model", "history_changed", "too_few_reviews", "rows_gone"]
)
def test_a_resume_point_that_no_longer_matches_starts_the_pass_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    mw = _resume_mw(
        monkeypatch,
        tmp_path,
        # the rows of one kind were deleted under the pass
        counted=(3, 1, 3) if case == "rows_gone" else (3, 2, 3),
    )
    review_ids = [100, 200, 300, 400, 500]
    if case == "history_changed":
        # a review inside the prefix was deleted, so the one at the resume
        # point is another review
        review_ids = [100, 300, 400, 500]
    if case == "too_few_reviews":
        review_ids = [100, 200]
    _write_resume_point(
        mw,
        reviews=3,
        last_review_id=300,
        rows=(3, 2, 3),
        model="another" if case == "other_model" else None,
    )

    assert rwkv_scheduler.rwkv_recordings_resume_point(mw, review_ids) is None


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: no step of the
# read is long. Every part of the query, and every block of the preparation
# that follows it, ends a step. Before this the preparation was one step:
# 11.9 s on 656,402 reviews, next to 0.3 s for a query part.
def test_every_step_of_the_history_read_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = [
        ((40 * 86_400 + 100 + index) * 1000, index, 10, 100, 2, 1234, 1, 3, 2500)
        for index in range(1, 200)
    ]
    monkeypatch.setattr(
        rwkv_scheduler, "RECORDINGS_PASS_PREPARE_STEP_ROWS", 16, raising=True
    )
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    steps: list[int] = []

    history = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        between_steps=lambda: steps.append(len(steps)),
    )

    assert len(history.reviews) == len(rows)
    # the 16 parts of the query, and then the preparation in blocks: far
    # more steps than the query alone would give
    assert len(steps) > rwkv_scheduler.HISTORY_QUERY_PARTS * 3, len(steps)

    # and a caller that passes nothing still gets the same reviews
    quiet = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    assert [review.identity.card_id for review in quiet.reviews] == [
        review.identity.card_id for review in history.reviews
    ]
    assert quiet.review_ids == history.review_ids
    assert quiet.history_hash == history.history_hash


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a pass that cannot
# save the curve sources refuses, as one that cannot record RWKV-Curve's
# values does. A silently dropped recorder let the pass walk the whole
# history writing no source, and card info then drew one segment for a card
# with years of reviews.
def test_a_pass_that_cannot_record_curve_sources_refuses() -> None:
    class _NoSources:
        resident_warm_up_state = True

        def warm_up_reviews(
            self,
            reviews: Sequence[RwkvReviewInput],
            *,
            review_ids: Sequence[int] | None = None,
            prediction_recorder: object = None,
            curve_recorder: object = None,
            progress: object = None,
            return_snapshot: bool = True,
        ) -> None:
            # a runtime that takes no curve_source_recorder: it would replay
            # the whole history and save no source at all
            raise AssertionError("the replay must not start")

    backend = RwkvStatefulReviewerBackend(_NoSources())
    with pytest.raises(rwkv_scheduler.RwkvCurveRecordingUnavailable):
        backend.warm_up(
            [],
            review_ids=[],
            curve_recorder=lambda review_id, value: None,
            curve_source_recorder=lambda sources: None,
        )


def _resume_mw(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    counted: tuple[int, int, int] = (3, 2, 3),
) -> SimpleNamespace:
    """A profile whose cache holds `counted` rows of the three kinds."""
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_model_cache_key", lambda: {"sha256": "abc"}
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(curve_source_tag=lambda: (1, 1)),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_replay_semantics_key", lambda reviewer, **_: "replay"
    )
    return SimpleNamespace(
        col=SimpleNamespace(db=SimpleNamespace(all=lambda sql, *args: [counted])),
        pm=SimpleNamespace(profileFolder=lambda: str(tmp_path)),
    )


def _write_resume_point(
    mw: object,
    *,
    reviews: int,
    last_review_id: int,
    rows: tuple[int, int, int],
    model: str | None = None,
) -> None:
    tag = rwkv_scheduler._rwkv_recordings_tag(mw)
    assert tag is not None
    rwkv_scheduler._write_rwkv_recordings_progress(
        mw,
        state="stopped",
        batches=7,
        reviews=reviews,
        lastReviewId=last_review_id,
        rows=list(rows),
        tag=dict(tag, model=model) if model else tag,
    )


# Pins spec/scheduling.md#sched.rwkv-recordings-progress
def test_the_pass_leaves_a_record_of_what_it_did(tmp_path: Path) -> None:
    mw = SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(tmp_path)))

    # nothing has run yet
    assert rwkv_scheduler.rwkv_recordings_progress(mw) is None

    rwkv_scheduler._write_rwkv_recordings_progress(mw, state="started", batches=0)
    started = rwkv_scheduler.rwkv_recordings_progress(mw)
    assert started is not None
    assert started["state"] == "started"
    assert isinstance(started["at"], int)

    # each step replaces the last, so the file says where the pass is now
    rwkv_scheduler._write_rwkv_recordings_progress(mw, state="running", batches=7)
    running = rwkv_scheduler.rwkv_recordings_progress(mw)
    assert running is not None
    assert (running["state"], running["batches"]) == ("running", 7)

    # and how it ended, whichever way it ended
    rwkv_scheduler._write_rwkv_recordings_progress(
        mw, state="stopped_for_review", batches=7, seconds=12.5
    )
    stopped = rwkv_scheduler.rwkv_recordings_progress(mw)
    assert stopped is not None
    assert stopped["state"] == "stopped_for_review"
    assert stopped["seconds"] == 12.5


# Pins spec/scheduling.md#sched.rwkv-recordings-progress: the record a screen
# reads is a whole record.
def test_the_record_and_the_marker_are_replaced_never_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`Path.write_text` empties the file before it writes it, and a screen
    reads this file while the pass writes it. Measured on this machine: with
    a plain write, 12,313 of 13,675 reads during a write could not be parsed;
    with a replace, 5 of 43,570. A reader that could not parse the record
    gets None, and a pass that reads None of its own resume point starts the
    whole history over."""
    mw = SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(tmp_path)))
    written: list[tuple[Path, bytes]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_atomic_write",
        lambda path, data: written.append((path, data)),
    )

    rwkv_scheduler._write_rwkv_recordings_progress(
        mw, state="running", batches=7, reviews=3, lastReviewId=300
    )
    assert len(written) == 1
    record = json.loads(written[0][1].decode("utf-8"))
    assert (record["state"], record["batches"], record["reviews"]) == ("running", 7, 3)
    assert written[0][0] == rwkv_scheduler._rwkv_recordings_progress_path(mw)

    rwkv_scheduler._write_rwkv_recordings_marker(
        mw, {"model": "a-model"}, reviews=3, last_review_id=300, rows=(3, 2, 3)
    )
    assert len(written) == 2
    marker = json.loads(written[1][1].decode("utf-8"))
    assert (marker["model"], marker["lastReviewId"], marker["rows"]) == (
        "a-model",
        300,
        [3, 2, 3],
    )
    assert written[1][0] == rwkv_scheduler._rwkv_recordings_marker_path(mw)

    # neither file was touched, because both writes went through the replace
    assert not list(tmp_path.rglob("*.json"))


# Pins spec/scheduling.md#sched.rwkv-recordings-progress: a record that cannot
# be written or read never fails the pass.
def test_an_unreadable_record_is_not_an_error(tmp_path: Path) -> None:
    mw = SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(tmp_path)))
    rwkv_scheduler._write_rwkv_recordings_progress(mw, state="started")
    path = rwkv_scheduler._rwkv_recordings_progress_path(mw)
    assert path is not None
    path.write_text("not json at all", encoding="utf-8")
    assert rwkv_scheduler.rwkv_recordings_progress(mw) is None

    # a window with no profile folder has nowhere to write, and says so
    nowhere = SimpleNamespace(pm=None)
    assert rwkv_scheduler._rwkv_recordings_progress_path(nowhere) is None
    rwkv_scheduler._write_rwkv_recordings_progress(nowhere, state="started")
    assert rwkv_scheduler.rwkv_recordings_progress(nowhere) is None


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a card on the
# screen stops the pass. While the pass runs it owns the replayed state, so a
# prediction cannot be served from it and the reviewer shows "Getting this
# card ready..." for as long as the pass lasts. Reviewing wins.
def test_a_card_on_the_screen_stops_the_pass() -> None:
    assert rwkv_scheduler._reviewer_is_showing_a_card(SimpleNamespace(state="review"))
    for elsewhere in ("deckBrowser", "overview", "browse", None):
        assert not rwkv_scheduler._reviewer_is_showing_a_card(
            SimpleNamespace(state=elsewhere)
        )
    # a window without the attribute at all is not the reviewer
    assert not rwkv_scheduler._reviewer_is_showing_a_card(SimpleNamespace())


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the rest between
# two batches is SHORT and BOUNDED, so the pass goes on while the user works.
# It used to wait for ten seconds of quiet, which every key press restarted:
# measured, the pass replayed 0 of 656,402 reviews in five minutes of use.
def test_the_pass_rests_a_bounded_time_between_batches() -> None:
    mw = SimpleNamespace(app=SimpleNamespace(last_input_at=0.0))
    batch = 0.2

    def rest(since_input: float | None) -> float:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                rwkv_scheduler,
                "_seconds_since_input",
                lambda _mw: since_input,
            )
            return rwkv_scheduler.recordings_pass_rest_seconds(mw, batch)

    # the user is typing: the pass rests a multiple of the batch it just did,
    # and then goes on. It never waits for the user to stop.
    assert rest(0.0) == batch * rwkv_scheduler.RECORDINGS_PASS_REST_RATIO
    assert rest(0.5) == batch * rwkv_scheduler.RECORDINGS_PASS_REST_RATIO
    # the user has left it alone: only long enough to hand a waiting click
    # the backend first
    assert rest(rwkv_scheduler.RECORDINGS_PASS_ACTIVE_SECS) == (
        rwkv_scheduler.RECORDINGS_PASS_MIN_REST_SECS
    )
    assert rest(None) == rwkv_scheduler.RECORDINGS_PASS_MIN_REST_SECS
    # and however long a batch took, the rest has a ceiling
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rwkv_scheduler, "_seconds_since_input", lambda _mw: 0.0)
        assert rwkv_scheduler.recordings_pass_rest_seconds(mw, 600.0) == (
            rwkv_scheduler.RECORDINGS_PASS_MAX_REST_SECS
        )


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: while it rests, the
# pass hands the RWKV backend back, so a click waits for one batch at most
# (measured: a click waited more than 30 s for it, the pass's whole length,
# before this).
def test_the_pass_hands_the_backend_back_while_it_rests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", SimpleNamespace())
    lock = rwkv_scheduler._reviewer_backend_execution_lock
    lock.acquire()
    try:
        with rwkv_scheduler._reviewer_backend_handed_back():
            # another thread can take the backend now
            taken = threading.Thread(target=_take_the_backend_lock)
            taken.start()
            taken.join(timeout=5.0)
            assert not taken.is_alive(), "the pass did not hand the backend back"
            # and a prediction made in that moment reads no half-replayed
            # state: it is refused and falls back
            assert rwkv_scheduler._reviewer_backend_resting.is_set()
            with rwkv_scheduler._try_reviewer_backend_prediction_access() as backend:
                assert backend is None
    finally:
        lock.release()
    assert not rwkv_scheduler._reviewer_backend_resting.is_set()
    # outside the rest the same access is granted
    with rwkv_scheduler._try_reviewer_backend_prediction_access() as backend:
        assert backend is not None


def _take_the_backend_lock() -> None:
    rwkv_scheduler._reviewer_backend_execution_lock.acquire()
    rwkv_scheduler._reviewer_backend_execution_lock.release()


def _the_backend_is_free() -> bool:
    """Whether another thread could take the RWKV backend right now."""
    taken: list[bool] = []

    def take() -> None:
        lock = rwkv_scheduler._reviewer_backend_execution_lock
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        taken.append(got)

    thread = threading.Thread(target=take)
    thread.start()
    thread.join()
    return taken[0]


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the pass reads the
# review history BEFORE it claims the RWKV backend. Reading it under the
# claim held the backend for the whole read, which is seconds of collection
# work on a large collection: measured, one click waited 9.9 s for it.
def test_the_pass_reads_the_history_before_it_claims_the_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    free: dict[str, bool] = {}

    class _WatchingRuntime(_CurveCacheRuntime):
        def review(self, **kwargs: Any) -> RwkvReviewTransition:
            free.setdefault("replay", _the_backend_is_free())
            return super().review(**kwargs)

    backend = RwkvStatefulReviewerBackend(_WatchingRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    original_history = rwkv_scheduler._historical_rwkv_review_inputs

    def reading_the_history(*args: Any, **kwargs: Any) -> Any:
        free.setdefault("history", _the_backend_is_free())
        return original_history(*args, **kwargs)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        reading_the_history,
    )

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    # free while the history is read, claimed while the replay runs
    assert free == {"history": True, "replay": False}


def _a_prediction_can_be_served() -> bool:
    """Whether ANOTHER thread could be served an RWKV prediction right now.

    Another thread, because the lock is reentrant and the pass's own thread
    inherits its claim: asked there, a shared-runtime pass would look free.
    """
    served: list[bool] = []

    def ask() -> None:
        with rwkv_scheduler._try_reviewer_backend_prediction_access() as backend:
            served.append(backend is not None)

    thread = threading.Thread(target=ask)
    thread.start()
    thread.join()
    return served[0]


class _WatchingCurveRuntime(_CurveCacheRuntime):
    """Records, at every review it replays, whether another thread could be
    served a prediction right then."""

    def __init__(self, served: list[bool]) -> None:
        super().__init__()
        self._served = served
        self.replayed = 0
        self.released = False

    def review(self, **kwargs: Any) -> RwkvReviewTransition:
        self.replayed += 1
        self._served.append(_a_prediction_can_be_served())
        return super().review(**kwargs)

    def release(self) -> None:
        self.released = True


class _TwinRuntimeBackend(RwkvStatefulReviewerBackend):
    """A backend that can load a second runtime, as the embedded one can."""

    def __init__(self, runtime_factory: Callable[[], Any]) -> None:
        super().__init__(runtime_factory())
        self._runtime_factory = runtime_factory
        self.loaded: list[_TwinRuntimeBackend] = []
        self.released = 0

    def new_runtime(self) -> _TwinRuntimeBackend:
        twin = _TwinRuntimeBackend(self._runtime_factory)
        self.loaded.append(twin)
        return twin

    def release_runtime(self) -> None:
        self.released += 1
        super().release_runtime()


def _a_recording_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    served: list[bool],
    *,
    own_runtime: bool,
) -> tuple[Any, SimpleNamespace]:
    """A collection of two reviews, and the backend the pass will use."""
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    def runtime_factory() -> _WatchingCurveRuntime:
        return _WatchingCurveRuntime(served)

    backend: Any
    if own_runtime:
        backend = _TwinRuntimeBackend(runtime_factory)
    else:
        # no `new_runtime`: the pass falls back to the shared runtime
        backend = RwkvStatefulReviewerBackend(runtime_factory())
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    reviewer.mw.col.review_prediction_rows.clear()
    # the warm-up above is the reviewer's own, not the pass's
    served.clear()
    backend._runtime.replayed = 0
    return backend, reviewer


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the pass replays
# into a model runtime of its OWN, and releases it when it is done. It used
# to replay into the shared one, which is why it had to claim it, rest
# against it and stop for a card on the screen.
def test_the_pass_replays_in_a_runtime_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    served: list[bool] = []
    shared, reviewer = _a_recording_pass(
        monkeypatch, tmp_path, served, own_runtime=True
    )

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    # exactly one runtime loaded for the pass
    assert len(shared.loaded) == 1
    own = shared.loaded[0]
    # and the replay ran in it, not in the shared one
    assert own._runtime.replayed > 0
    assert shared._runtime.replayed == 0


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: a prediction asked
# for WHILE the pass runs is served. That is the whole point of the pass
# having a runtime of its own; before it, the answer was no for the pass's
# whole length.
def test_a_prediction_is_served_while_the_pass_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    served: list[bool] = []
    _shared, reviewer = _a_recording_pass(
        monkeypatch, tmp_path, served, own_runtime=True
    )

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    assert served, "the pass replayed nothing"
    assert all(served), "a prediction was refused while the pass replayed"
    # and nothing was left set behind the pass
    assert not rwkv_scheduler._reviewer_backend_resting.is_set()
    assert _a_prediction_can_be_served()


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the pass releases
# its own runtime as soon as it is done, so neither the weights nor the state
# it replayed stay in memory behind it. It releases it when the pass fails
# too.
def test_the_pass_releases_its_own_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    served: list[bool] = []
    shared, reviewer = _a_recording_pass(
        monkeypatch, tmp_path, served, own_runtime=True
    )

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    own = shared.loaded[0]
    assert own.released == 1
    assert own._runtime.released is True
    # the shared runtime is left alone
    assert shared.released == 0
    assert shared._runtime.released is False

    # a pass that fails releases it too
    def failing_step(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the fold roles could not be read")

    shared.loaded.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            rwkv_scheduler,
            "_rwkv_calibration_fold_role_maps",
            failing_step,
        )
        assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is False
    assert len(shared.loaded) == 1
    assert shared.loaded[0].released == 1


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the runtime the
# pass loads is the SAME model as the reviewer's - the same weights file and
# the same settings - so the rows it records are the rows the shared runtime
# would have recorded.
def test_its_own_runtime_is_the_same_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aqt import rwkv_srs_benchmark

    loaded: list[dict[str, Any]] = []

    class _FakeRustRuntime:
        resident_warm_up_state = True

        def __init__(self, **kwargs: Any) -> None:
            loaded.append(kwargs)

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", _FakeRustRuntime)
    model = tmp_path / "model.safetensors"
    backend = rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend(
        model_path=model,
        target_retention=0.87,
        max_interval_days=1234,
    )

    second = backend.new_runtime()

    assert isinstance(second, rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend)
    assert second is not backend
    assert loaded[1] == loaded[0]
    assert loaded[0] == {
        "model_path": model,
        "target_retention": 0.87,
        "max_interval_days": 1234,
    }


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: the rows the pass
# records are the same rows either way. A runtime of its own changes who
# replays, never what is written.
def test_the_pass_records_the_same_rows_in_either_runtime(
    tmp_path: Path,
) -> None:
    rows: dict[bool, list[tuple[Any, ...]]] = {}
    for own_runtime in (False, True):
        with pytest.MonkeyPatch.context() as patch:
            folder = tmp_path / ("own" if own_runtime else "shared")
            folder.mkdir()
            _backend, reviewer = _a_recording_pass(
                patch, folder, [], own_runtime=own_runtime
            )
            assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True
            rows[own_runtime] = list(reviewer.mw.col.review_prediction_rows)

    assert rows[True] == rows[False]
    assert rows[True], "the pass recorded nothing at all"


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic, the LIMITATION: a
# backend that cannot load a second runtime falls back to the shared one,
# claimed and restored as before. A prediction asked for then is refused, and
# a card on the screen really does have to stop the pass.
def test_a_backend_that_cannot_load_a_second_runtime_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    served: list[bool] = []
    backend, reviewer = _a_recording_pass(
        monkeypatch, tmp_path, served, own_runtime=False
    )
    assert not hasattr(backend, "new_runtime")

    assert rwkv_scheduler.recompute_rwkv_calibration_data(reviewer.mw) is True

    assert served, "the pass replayed nothing"
    assert not any(served), "the shared-runtime pass served a prediction"
    # and the shared runtime is the one that replayed
    assert backend._runtime.replayed > 0
    # the claim is given back at the end
    assert _a_prediction_can_be_served()


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic
def test_the_pass_runs_in_the_background_without_a_progress_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[object] = []
    ran: list[object] = []

    def pass_body(mw: object, **_kwargs: object) -> bool:
        ran.append(mw)
        assert rwkv_scheduler.rwkv_recordings_pass_running() is True
        return True

    monkeypatch.setattr(rwkv_scheduler, "recompute_rwkv_calibration_data", pass_body)
    mw = _recording_pass_mw(windows)

    rwkv_scheduler.recompute_rwkv_calibration_data_in_background(mw)
    _join_the_recording_pass()

    assert windows == [], "the pass must not open a progress window"
    assert ran == [mw]

    with pytest.MonkeyPatch.context() as patch:
        shown: list[str] = []
        patch.setattr("aqt.utils.tooltip", lambda msg, **kw: shown.append(msg))
        # the translations need a collection, which this test has not
        patch.setattr(
            rwkv_scheduler,
            "_tr",
            lambda: SimpleNamespace(qt_misc_stats_data_ready=lambda: "ready"),
        )
        for callback in mw.taskman.on_main:
            callback()
    assert shown == ["ready"]
    assert rwkv_scheduler.rwkv_recordings_pass_running() is False


def _recording_pass_mw(windows: list[object]) -> SimpleNamespace:
    """An `mw` whose collection worker fails the test if the pass takes it."""
    taskman = SimpleNamespace(
        run_in_background=lambda *args, **kwargs: pytest.fail(
            "the recording pass must not take the collection worker"
        ),
        with_progress=lambda *args, **kwargs: windows.append(args),
        on_main=[],
    )
    taskman.run_on_main = taskman.on_main.append
    return SimpleNamespace(
        col=SimpleNamespace(),
        taskman=taskman,
        progress=SimpleNamespace(timer=lambda *a, **k: None),
    )


def _join_the_recording_pass() -> None:
    for thread in threading.enumerate():
        if thread.name == "rwkv-recordings-pass":
            thread.join(timeout=10.0)
            assert not thread.is_alive()


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic: there is ONE
# collection worker, and answering a card, clicking a deck, the deck list,
# the Browser and the Stats all go through it. A pass that walks the whole
# history on that worker put every one of them behind it for tens of
# minutes (measured: a small collection operation waited 80.4 s behind a
# 20,000-review pass).
def test_the_recording_pass_leaves_the_collection_worker_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[object] = []
    threads: list[str] = []

    def pass_body(_mw: object, **_kwargs: object) -> bool:
        threads.append(threading.current_thread().name)
        return False

    monkeypatch.setattr(rwkv_scheduler, "recompute_rwkv_calibration_data", pass_body)
    mw = _recording_pass_mw(windows)

    # `_recording_pass_mw` fails the test from inside `run_in_background`
    rwkv_scheduler.recompute_rwkv_calibration_data_in_background(mw)
    _join_the_recording_pass()

    assert threads == ["rwkv-recordings-pass"]
    assert rwkv_scheduler.rwkv_recordings_pass_running() is False


# The pass stops by itself once the profile it started in has closed, so it
# never walks a collection that is being torn down. Both of its own hooks
# say so: the one that reports progress and the one it rests in. The rest
# used to be a wait for the user with no such check, and a pass that was
# waiting in it never noticed the profile closing at all.
def test_the_recording_pass_stops_when_the_profile_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[object] = []
    mw = _recording_pass_mw(windows)
    rested: list[float] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "recordings_pass_rest_seconds",
        lambda _mw, batch: rested.append(batch) or 0.0,
    )

    def pass_body(
        _mw: object,
        *,
        progress: object = None,
        between_batches: object = None,
        **_kwargs: object,
    ) -> bool:
        assert callable(progress)
        assert callable(between_batches)
        progress("still open", None, None)
        between_batches()
        mw.col = None  # the profile closes under the pass
        with pytest.raises(rwkv_scheduler._ReviewerBackendWarmupInvalidated):
            progress("closed", None, None)
        with pytest.raises(rwkv_scheduler._ReviewerBackendWarmupInvalidated):
            between_batches()
        return False

    monkeypatch.setattr(rwkv_scheduler, "recompute_rwkv_calibration_data", pass_body)

    rwkv_scheduler.recompute_rwkv_calibration_data_in_background(mw)
    _join_the_recording_pass()

    assert len(rested) == 1


def _state_cache_read_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata: dict[str, object],
) -> tuple[list[tuple[int, ...]], list[str], object]:
    """The state-cache read with its two cheap validations watched.

    Returns the ignored-id tuples the Rust fingerprint was asked for, the
    names of the paths that ran, and the object the fingerprint hands back.
    """
    fingerprint_calls: list[tuple[int, ...]] = []
    paths: list[str] = []
    restored = cast(Any, object())

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_binary_location",
        lambda _reviewer: (tmp_path, metadata),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_metadata_compatible",
        lambda *_args, **_kwargs: True,
    )
    # a sync always changes the collection, so this marker never matches
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_collection_unchanged",
        lambda *_args, **_kwargs: False,
    )

    def fingerprint(
        _reviewer: object,
        *,
        backend: object,
        cache_dir: Path,
        metadata: dict[str, object],
        ignored_review_ids: tuple[int, ...],
    ) -> object:
        fingerprint_calls.append(ignored_review_ids)
        paths.append("fingerprint")
        return restored

    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache_from_rust_fingerprint", fingerprint
    )

    def whole_history(*_args: object, **_kwargs: object) -> object:
        paths.append("whole history")
        raise _StopTheWholeHistoryRead()

    monkeypatch.setattr(
        rwkv_scheduler, "_historical_rwkv_review_inputs", whole_history
    )
    return fingerprint_calls, paths, restored


class _StopTheWholeHistoryRead(Exception):
    """Raised instead of reading every review, so the test can see it happen."""


def test_a_sync_of_recent_reviews_validates_the_cache_without_reading_it_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recent synchronized review changes no ignored id, so the cheap
    validation speaks for the read.

    A review newer than `_RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS` is
    dropped from the ignored set again, so the set the read works with is the
    set the cache already holds. Reading all 656k reviews to learn that cost
    Andrew about 30 seconds behind a window after every sync (B-012).
    """
    last_review_id = 2_000_000_000_000
    metadata: dict[str, object] = {
        "lastReviewId": last_review_id,
        "reviewCount": 2,
        "historyHash": "b" * 64,
        "replayKey": "replay-key",
    }
    fingerprint_calls, paths, restored = _state_cache_read_harness(
        monkeypatch, tmp_path, metadata
    )

    stored = rwkv_scheduler._read_rwkv_state_cache_binary(
        SimpleNamespace(),
        backend=cast(Any, object()),
        additional_ignored_review_ids=(last_review_id - 3 * 86_400_000,),
    )

    assert stored is restored
    assert paths == ["fingerprint"]
    # the cache's own ignored ids, and nothing added
    assert fingerprint_calls == [()]


def test_a_sync_of_old_reviews_still_reads_the_whole_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The other half of the rule above: a review old enough to be ignored
    does change the set, and then only the whole history can say what the
    history now is."""
    last_review_id = 2_000_000_000_000
    metadata: dict[str, object] = {
        "lastReviewId": last_review_id,
        "reviewCount": 2,
        "historyHash": "b" * 64,
        "replayKey": "replay-key",
    }
    _fingerprint_calls, paths, _restored = _state_cache_read_harness(
        monkeypatch, tmp_path, metadata
    )

    # nine days old, past the eight-day cutoff
    old_review_id = last_review_id - 9 * 86_400_000
    assert (
        rwkv_scheduler._read_rwkv_state_cache_binary(
            SimpleNamespace(),
            backend=cast(Any, object()),
            additional_ignored_review_ids=(old_review_id,),
        )
        is None
    )
    assert paths == ["whole history"]


# Andrew, 2026-09-21 (report B-011): "the first click on a deck has a MASSIVE
# delay, like 1-3 seconds." Measured at 2314 ms on a 656,402-review collection.
# The task manager has ONE collection worker and runs one task at a time, so
# the start-up restore held it with the click's own work queued behind it.
def test_the_startup_load_stays_off_the_collection_worker_but_still_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    users = [0]
    users_while_running: list[int] = []
    users_at_done: list[int] = []

    class Taskman:
        def collection_use_started(self) -> None:
            users[0] += 1

        def collection_use_finished(self, *_args: object) -> None:
            users[0] -= 1

        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool = True,
        ) -> None:
            calls.append({"uses_collection": uses_collection})
            future: Future[Any] = Future()
            future.set_result(task())
            on_done(future)

    def task() -> str:
        # a periodic backup asks `collection_busy()`, which reads this count
        # (spec ui.periodic-backup-waits)
        users_while_running.append(users[0])
        return "done"

    def on_done(future: Future[object]) -> None:
        users_at_done.append(users[0])
        assert future.result() == "done"

    mw = SimpleNamespace(taskman=Taskman())
    rwkv_scheduler._run_in_background(mw, task, on_done)

    # off the one collection worker, so a click does not queue behind it
    assert calls == [{"uses_collection": False}]
    # and still counted as collection use while it runs
    assert users_while_running == [1]
    assert users_at_done == [0]
    assert users[0] == 0


def test_a_startup_load_that_cannot_start_leaves_no_collection_user_behind() -> None:
    users = [0]

    class Taskman:
        def collection_use_started(self) -> None:
            users[0] += 1

        def collection_use_finished(self, *_args: object) -> None:
            users[0] -= 1

        def run_in_background(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("no worker")

    mw = SimpleNamespace(taskman=Taskman())
    with pytest.raises(RuntimeError):
        rwkv_scheduler._run_in_background(mw, lambda: None, lambda _f: None)
    # otherwise a periodic backup would wait for work that never started
    assert users[0] == 0


# Andrew, 2026-09-22 (report B-014): switching the two-button mode during a
# review showed "Getting this card ready..." over the answer buttons for about
# ten seconds. `main.py` routes every config, deck, deck-config and notetype
# change to fsrs_preset_resolution_did_change, so saving Preferences arrived as
# a reason to throw the resident RWKV state away and build it again.
def test_a_config_change_the_replay_cannot_see_keeps_the_resident_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity(replay_key="canonical-replay")
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )
    generation_before = rwkv_scheduler._reviewer_backend_warmup_generations.get(
        warmup_key, 0
    )

    # the collection still replays exactly as the resident state was built
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda *_args, **_kwargs: "canonical-replay",
    )

    try:
        rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

        # kept, so the next card is predicted at once instead of after a
        # rebuild
        assert rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] is (
            resident_identity
        )
        assert (
            rwkv_scheduler._reviewer_backend_warmup_generations.get(warmup_key, 0)
            == generation_before
        )
    finally:
        # this is the one test here that leaves resident state behind on
        # purpose, and the dicts are module-level: a later test would find a
        # warmed-up state that its own collection never built
        rwkv_scheduler._reviewer_backend_warmup_states.pop(warmup_key, None)
        rwkv_scheduler._rwkv_memorised_history_identity_cache.pop(warmup_key, None)


def test_a_change_that_alters_the_replay_still_discards_the_resident_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: moving a deck to another preset changes the replay, and
    the state built under the old one must go."""
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    resident_identity = _rwkv_resident_identity(replay_key="canonical-replay")
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = resident_identity
    rwkv_scheduler._rwkv_memorised_history_identity_cache[warmup_key] = (
        0,
        resident_identity,
    )

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_replay_semantics_key",
        lambda *_args, **_kwargs: "the-presets-moved",
    )

    rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states
    assert warmup_key not in rwkv_scheduler._rwkv_memorised_history_identity_cache
    assert rwkv_scheduler._reviewer_backend_warmup_generations[warmup_key] == 1


def test_a_replay_key_that_cannot_be_read_discards_the_resident_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable semantics are treated as changed semantics: a slow rebuild
    is a cost, a stale state is a wrong interval."""
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = SimpleNamespace()
    warmup_key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert warmup_key is not None
    rwkv_scheduler._reviewer_backend_warmup_states[warmup_key] = (
        _rwkv_resident_identity(replay_key="canonical-replay")
    )

    def explode(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("no deck configs")

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_replay_semantics_key", explode)

    rwkv_scheduler.fsrs_preset_resolution_did_change(reviewer.mw)

    assert warmup_key not in rwkv_scheduler._reviewer_backend_warmup_states

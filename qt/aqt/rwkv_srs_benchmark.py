# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import array
import importlib.util
import logging
import math
import struct
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from aqt.rwkv_scheduler import (
    RwkvBackendCacheSnapshot,
    RwkvButtonProbabilities,
    RwkvCurveRecordingUnavailable,
    RwkvCurveSourceRecorder,
    RwkvCurveSources,
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewCandidate,
    RwkvReviewerBackend,
    RwkvReviewerStateSnapshot,
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewPredictionRequest,
    RwkvReviewTransition,
    RwkvStateCacheSnapshotCallback,
    RwkvStatefulReviewerBackend,
    RwkvWarmUpProgress,
    RwkvWarmUpProgressCallback,
    _rwkv_state_update_input,
    interval_from_recall_curve,
    rwkv_review_identity,
    rwkv_review_input,
    unrounded_interval_from_recall_curve,
)

logger = logging.getLogger(__name__)

_PACKED_PREDICTION_REQUEST_MAGIC = b"ARWKVPR2"
_PACKED_WARM_UP_REVIEW_MAGIC = b"ARWKVWU2"
_PACKED_PREDICTION_REQUEST_HEADER = struct.Struct("<8sI")
_PACKED_PREDICTION_REQUEST_ROW = struct.Struct("<IqqqqBBqqqqqffffB")
_RUST_WARMUP_CHUNK_SIZE = 16_384
_RUST_STATE_ONLY_WARMUP_CHUNK_SIZE = 16_384


class SrsBenchmarkRwkvReviewerBackend(RwkvReviewerBackend):
    """Optional bridge to the RWKV RNN runner from srs-benchmark."""

    def __init__(
        self,
        *,
        benchmark_path: str | Path | None = None,
        model_path: str | Path | None = None,
        device: str = "cpu",
        dtype: str = "float",
        target_retention: float = 0.9,
        max_interval_days: int = 36500,
        process: object | None = None,
        row_factory: Callable[[dict[str, object]], object] | None = None,
    ) -> None:
        if process is None:
            if benchmark_path is None or model_path is None:
                raise ValueError("benchmark_path and model_path are required")
            process, row_factory = _load_srs_benchmark_process(
                benchmark_path=Path(benchmark_path),
                model_path=Path(model_path),
                device=device,
                dtype=dtype,
            )

        self._process: Any = process
        self._row_builder = SrsBenchmarkReviewRowBuilder(row_factory or dict)
        self._target_retention = target_retention
        self._max_interval_days = max_interval_days
        self._curves: dict[int, object] = {}

    def warm_up(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        progress: RwkvWarmUpProgressCallback | None = None,
    ) -> None:
        total = len(reviews)
        report_every = _warmup_progress_interval(total)
        _report_warmup_progress(progress, processed=0, total=total)

        for processed, review_input in enumerate(reviews, start=1):
            if review_input.ease is None:
                if processed == total or processed % report_every == 0:
                    _report_warmup_progress(
                        progress,
                        processed=processed,
                        total=total,
                    )
                continue

            if prediction_recorder is not None and review_ids is not None:
                review_id = (
                    review_ids[processed - 1]
                    if processed - 1 < len(review_ids)
                    else None
                )
                if isinstance(review_id, int):
                    probability = self._process.imm_predict(
                        self._row_builder.row_for(
                            replace(
                                review_input,
                                is_query=True,
                                ease=None,
                                duration_millis=None,
                            )
                        )
                    )
                    prediction_recorder(review_id, _probability_as_float(probability))

            curve = self._process.process_row(
                self._row_builder.row_for(_rwkv_state_update_input(review_input))
            )
            if curve is not None:
                self._curves[review_input.identity.card_id] = curve

            if processed == total or processed % report_every == 0:
                _report_warmup_progress(progress, processed=processed, total=total)

    def predict_review(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return None

        review_input = rwkv_review_input(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=None,
        )
        probability = self._process.imm_predict(self._row_builder.row_for(review_input))
        intervals = self._interval_overrides(review_input)
        s90s = self._s90_overrides(review_input)
        return RwkvReviewPrediction(
            retrievability=_probability_as_float(probability),
            curve_retrievability=self._curve_retrievability(review_input),
            current_interval=_whole_interval(intervals.good),
            current_s90=s90s.good,
            interval_overrides=intervals,
            s90_overrides=s90s,
        )

    def predict_review_retrievability(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return None

        review_input = rwkv_review_input(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=None,
        )
        probability = self._process.imm_predict(self._row_builder.row_for(review_input))
        return RwkvReviewPrediction(retrievability=_probability_as_float(probability))

    def predict_reviews(
        self,
        candidates: Sequence[RwkvReviewCandidate],
    ) -> Sequence[RwkvReviewPrediction | None]:
        inputs_by_index: list[tuple[int, RwkvReviewInput]] = []
        rows = []
        predictions: list[RwkvReviewPrediction | None] = [None] * len(candidates)

        for index, candidate in enumerate(candidates):
            identity = rwkv_review_identity(candidate.reviewer, candidate.card)
            if identity is None:
                continue

            review_input = rwkv_review_input(
                reviewer=candidate.reviewer,
                card=candidate.card,
                identity=identity,
                ease=None,
            )
            inputs_by_index.append((index, review_input))
            rows.append(self._row_builder.row_for(review_input))

        if not rows:
            return predictions

        imm_predict_many = getattr(self._process, "imm_predict_many", None)
        probabilities = (
            imm_predict_many(rows)
            if callable(imm_predict_many)
            else [self._process.imm_predict(row) for row in rows]
        )

        for (index, review_input), probability in zip(
            inputs_by_index, probabilities, strict=True
        ):
            intervals = self._interval_overrides(review_input)
            s90s = self._s90_overrides(review_input)
            predictions[index] = RwkvReviewPrediction(
                retrievability=_probability_as_float(probability),
                curve_retrievability=self._curve_retrievability(review_input),
                current_interval=_whole_interval(intervals.good),
                current_s90=s90s.good,
                interval_overrides=intervals,
                s90_overrides=s90s,
            )

        return predictions

    def review_answered(
        self,
        *,
        reviewer: object,
        card: object,
        ease: int,
    ) -> None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return

        review_input = rwkv_review_input(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=ease,
        )
        curve = self._process.process_row(
            self._row_builder.row_for(_rwkv_state_update_input(review_input))
        )
        if curve is not None:
            self._curves[identity.card_id] = curve

    def _interval_overrides(
        self, review_input: RwkvReviewInput
    ) -> RwkvIntervalOverride:
        # unrounded, possibly under a day (spec sched.sub-day-intervals)
        return self._curve_interval_overrides(
            review_input,
            review_input.target_retentions,
            unrounded=True,
        )

    def _s90_overrides(self, review_input: RwkvReviewInput) -> RwkvIntervalOverride:
        # unrounded, possibly under a day (spec sched.rwkv-curve-s90)
        return self._curve_interval_overrides(
            review_input,
            (0.9, 0.9, 0.9, 0.9),
            unrounded=True,
        )

    def _curve_retrievability(self, review_input: RwkvReviewInput) -> float | None:
        curve = self._curves.get(review_input.identity.card_id)
        if curve is None:
            return None

        elapsed_seconds = review_input.current_elapsed_seconds
        if elapsed_seconds is None or elapsed_seconds < 0:
            elapsed_days = review_input.current_elapsed_days
            if elapsed_days is None or elapsed_days < 0:
                return None
            elapsed_seconds = elapsed_days * 86_400

        return _probability_as_float(self._process.predict_func(curve, elapsed_seconds))

    def _curve_interval_overrides(
        self,
        review_input: RwkvReviewInput,
        target_retentions: tuple[float | None, ...],
        *,
        unrounded: bool = False,
    ) -> RwkvIntervalOverride:
        curve = self._curves.get(review_input.identity.card_id)
        if curve is None:
            return RwkvIntervalOverride()

        search_days: list[float] = list(_interval_search_days(self._max_interval_days))
        if unrounded:
            search_days = list(_SUB_DAY_SEARCH_POINTS) + search_days
        points = [
            RwkvRecallPoint(
                elapsed_days=day,
                retrievability=_probability_as_float(
                    self._process.predict_func(curve, day * 86_400)
                ),
            )
            for day in search_days
        ]

        find_interval = (
            unrounded_interval_from_recall_curve
            if unrounded
            else interval_from_recall_curve
        )
        intervals = [
            find_interval(
                points,
                target_retention=_valid_target_retention(
                    target_retention,
                    fallback=self._target_retention,
                ),
                max_interval_days=self._max_interval_days,
            )
            for target_retention in target_retentions
        ]
        return RwkvIntervalOverride(
            again=intervals[0],
            hard=intervals[1],
            good=intervals[2],
            easy=intervals[3],
        )


def _valid_target_retention(value: object, *, fallback: float) -> float:
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    ):
        return float(value)
    return fallback


class EmbeddedRwkvReviewerBackend(RwkvStatefulReviewerBackend):
    """RWKV backend using Anki's embedded Rust inference-only runner."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "cpu",
        dtype: str = "float",
        target_retention: float = 0.9,
        max_interval_days: int = 36500,
    ) -> None:
        del device, dtype
        self._model_path = Path(model_path)
        self._target_retention = target_retention
        self._max_interval_days = max_interval_days
        super().__init__(
            _RustRwkvRuntime(
                model_path=self._model_path,
                target_retention=target_retention,
                max_interval_days=max_interval_days,
            ),
        )

    def new_runtime(self) -> "EmbeddedRwkvReviewerBackend":
        """A second runtime of the same model: the same weights file, the
        same settings, the same entry point, and a state of its own.

        The recording pass replays into one of these, so the reviewer keeps
        the shared runtime and answers a card while the pass runs (spec
        sched.rwkv-recordings-automatic).
        """
        return EmbeddedRwkvReviewerBackend(
            model_path=self._model_path,
            target_retention=self._target_retention,
            max_interval_days=self._max_interval_days,
        )


class _RustRwkvRuntime:
    resident_warm_up_state = True

    def __init__(
        self,
        *,
        model_path: Path,
        target_retention: float,
        max_interval_days: int,
    ) -> None:
        from anki import _rsbridge

        rwkv_inference = getattr(_rsbridge, "RwkvInference")
        self._process = rwkv_inference(
            str(model_path),
            target_retention,
            max_interval_days,
        )
        self._process_lock = threading.RLock()
        # a constant of the model: asked once, never under the process lock
        self._curve_source_tag = self._read_curve_source_tag()

    def release(self) -> None:
        """Drop the model and everything it holds.

        The recording pass owns a runtime of its own and releases it as soon
        as it is done, so the weights and the state it replayed do not stay
        in memory behind it.
        """
        with self._locked_process():
            process = self._process
            reset = getattr(process, "reset_warm_up_state", None)
            if callable(reset):
                # the states first, without the GIL: dropping a whole-history
                # state with the process held every Python thread for 2.2 s
                reset()
            self._process = None

    def _locked_process(self) -> Any:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._process_lock = lock
        return lock

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
        with self._locked_process():
            (
                retrievability,
                curve_retrievability,
                current_interval,
                current_s90,
                intervals,
                s90s,
                button_probabilities,
                next_card_state,
                next_note_state,
                next_deck_state,
                next_preset_state,
                next_global_state,
            ) = self._process.review(
                identity.card_id,
                identity.note_id,
                identity.deck_id,
                identity.preset_id,
                review_input.is_query,
                review_input.ease,
                review_input.duration_millis,
                review_input.card_type,
                review_input.day_offset,
                review_input.current_elapsed_days,
                review_input.current_elapsed_seconds,
                *review_input.target_retentions,
                _state_bytes(card_state),
                _state_bytes(note_state),
                _state_bytes(deck_state),
                _state_bytes(preset_state),
                _state_bytes(global_state),
                review_input.enforce_grade_order,
            )

        return RwkvReviewTransition(
            prediction=RwkvReviewPrediction(
                retrievability=float(retrievability),
                curve_retrievability=(
                    float(curve_retrievability)
                    if curve_retrievability is not None
                    else None
                ),
                current_interval=_optional_interval(current_interval),
                current_s90=_optional_unrounded_interval(current_s90),
                interval_overrides=_unrounded_interval_override_from_tuple(intervals),
                s90_overrides=_unrounded_interval_override_from_tuple(s90s),
                button_probabilities=_button_probabilities_from_tuple(
                    button_probabilities
                ),
            ),
            card_state=next_card_state,
            note_state=next_note_state,
            deck_state=next_deck_state,
            preset_state=next_preset_state,
            global_state=next_global_state,
        )

    def warm_up_reviews(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        curve_recorder: Callable[[int, float], None] | None = None,
        curve_source_recorder: RwkvCurveSourceRecorder | None = None,
        progress: RwkvWarmUpProgressCallback | None = None,
        snapshot_after_reviews: Sequence[int] = (),
        snapshot_recorder: RwkvStateCacheSnapshotCallback | None = None,
        return_snapshot: bool = True,
        batch_rows: int | None = None,
    ) -> RwkvBackendCacheSnapshot | None:
        total = len(reviews)
        snapshot_endpoints = sorted(
            {endpoint for endpoint in snapshot_after_reviews if 0 < endpoint <= total}
        )
        record_predictions = prediction_recorder is not None and review_ids is not None
        backend_chunk_size = _rust_warmup_chunk_size(
            total,
            record_predictions=record_predictions,
            batch_rows=batch_rows,
        )
        _report_warmup_progress(progress, processed=0, total=total)
        warm_up_packed = getattr(self._process, "warm_up_reviews_packed", None)
        # each answered review's curve source, for card info (spec
        # ui.card-info-rwkv-curve): the replay copies it as it goes
        record_sources = curve_source_recorder is not None and review_ids is not None
        if record_sources and not callable(
            getattr(self._process, "record_curve_sources", None)
        ):
            # say so rather than replay the whole history saving no source:
            # card info would then draw one segment per card and nothing
            # would explain why
            raise RwkvCurveRecordingUnavailable(
                "this RWKV runtime cannot record curve sources, so the "
                "curves card info draws cannot be saved"
            )

        self._warm_up_batches(
            reviews,
            review_ids=review_ids,
            prediction_recorder=prediction_recorder,
            curve_recorder=curve_recorder,
            curve_source_recorder=(curve_source_recorder if record_sources else None),
            progress=progress,
            snapshot_endpoints=snapshot_endpoints,
            snapshot_recorder=snapshot_recorder,
            backend_chunk_size=backend_chunk_size,
            record_predictions=record_predictions,
            record_sources=record_sources,
            warm_up_packed=warm_up_packed,
        )
        if not return_snapshot:
            return None
        with self._locked_process():
            return self._warm_up_snapshot_locked()

    def _warm_up_batches(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None,
        prediction_recorder: Callable[[int, float], None] | None,
        curve_recorder: Callable[[int, float], None] | None,
        curve_source_recorder: RwkvCurveSourceRecorder | None,
        progress: RwkvWarmUpProgressCallback | None,
        snapshot_endpoints: Sequence[int],
        snapshot_recorder: RwkvStateCacheSnapshotCallback | None,
        backend_chunk_size: int,
        record_predictions: bool,
        record_sources: bool,
        warm_up_packed: Any,
    ) -> None:
        """Replays `reviews` one batch at a time.

        The runtime lock is taken per batch, not once for the whole replay,
        so a caller that replays the whole review history does not hold the
        runtime for minutes. The replayed state lives in the runtime, so a
        batch goes on where the one before it stopped, and whoever paces
        this replay can step aside for the user between two batches (spec
        sched.rwkv-recordings-automatic).
        """
        total = len(reviews)
        processed = 0
        endpoint_index = 0
        while processed < total:
            chunk_end = min(processed + backend_chunk_size, total)
            if endpoint_index < len(snapshot_endpoints):
                chunk_end = min(chunk_end, snapshot_endpoints[endpoint_index])
            chunk = reviews[processed:chunk_end]
            with self._locked_process():
                if record_sources:
                    self._process.record_curve_sources(True)
                try:
                    if callable(warm_up_packed):
                        predictions = warm_up_packed(
                            _packed_warm_up_reviews(chunk),
                            record_predictions,
                        )
                    else:
                        predictions = self._process.warm_up_reviews(
                            [_review_input_row(review_input) for review_input in chunk],
                            record_predictions,
                        )
                    if (
                        record_predictions
                        and review_ids is not None
                        and prediction_recorder is not None
                    ):
                        _record_warm_up_predictions(
                            prediction_recorder,
                            review_ids,
                            processed,
                            predictions,
                            curve_recorder=curve_recorder,
                        )
                    if curve_source_recorder is not None and review_ids is not None:
                        sources = _recorded_curve_sources(
                            self._process,
                            review_ids,
                            processed,
                        )
                        if sources is not None:
                            curve_source_recorder(sources)

                    processed += len(chunk)
                    if (
                        endpoint_index < len(snapshot_endpoints)
                        and processed == snapshot_endpoints[endpoint_index]
                    ):
                        if snapshot_recorder is not None:
                            write_runtime_checkpoint = getattr(
                                snapshot_recorder,
                                "write_runtime_checkpoint",
                                None,
                            )
                            write_runtime_snapshot = getattr(
                                snapshot_recorder,
                                "write_runtime_snapshot",
                                None,
                            )
                            if callable(write_runtime_checkpoint):
                                write_runtime_checkpoint(
                                    processed,
                                    self.write_warm_up_state_checkpoint,
                                )
                            elif callable(write_runtime_snapshot):
                                write_runtime_snapshot(
                                    processed,
                                    self.append_warm_up_snapshot_binary,
                                )
                            else:
                                snapshot_recorder(
                                    processed,
                                    self._warm_up_snapshot_locked(),
                                )
                        endpoint_index += 1
                finally:
                    if record_sources:
                        self._process.record_curve_sources(False)
            # outside the runtime lock: whoever paces the replay rests here
            _report_warmup_progress(progress, processed=processed, total=total)

    def _warm_up_snapshot_locked(self) -> RwkvBackendCacheSnapshot:
        (
            card_states,
            note_states,
            deck_states,
            preset_states,
            global_state,
            runtime_state,
        ) = self._process.warm_up_snapshot()
        return RwkvBackendCacheSnapshot(
            card_states=dict(card_states),
            note_states=dict(note_states),
            deck_states=dict(deck_states),
            preset_states=dict(preset_states),
            global_state=global_state,
            runtime_state=runtime_state,
        )

    def warm_up_snapshot(self) -> RwkvBackendCacheSnapshot:
        with self._locked_process():
            return self._warm_up_snapshot_locked()

    def append_warm_up_snapshot_binary(self, path: Path) -> None:
        with self._locked_process():
            self._process.append_warm_up_snapshot_binary(str(path))

    def write_warm_up_state_checkpoint(
        self,
        path: Path,
        store_generation: str,
        parent_segment_id: int | None,
        last_review_id: int,
        review_count: int,
        history_hash: str,
        replay_key: str,
        previous_review_ids: bytes,
        previous_intervals: bytes,
        review_counts: bytes,
        full: bool,
        durable: bool,
    ) -> int:
        with self._locked_process():
            return cast(
                int,
                self._process.write_warm_up_state_checkpoint(
                    str(path),
                    store_generation,
                    parent_segment_id,
                    last_review_id,
                    review_count,
                    history_hash,
                    replay_key,
                    previous_review_ids,
                    previous_intervals,
                    review_counts,
                    full,
                    durable,
                ),
            )

    def finish_warm_up_state_checkpoints(self) -> None:
        finish = getattr(self._process, "finish_warm_up_state_checkpoints", None)
        if callable(finish):
            with self._locked_process():
                finish()

    def restore_warm_up_state_checkpoint(
        self,
        path: Path,
        store_generation: str,
        segment_id: int,
    ) -> None:
        with self._locked_process():
            self._process.restore_warm_up_state_checkpoint(
                str(path),
                store_generation,
                segment_id,
            )

    def warm_up_state(
        self,
        review_input: RwkvReviewInput,
    ) -> RwkvReviewerStateSnapshot:
        with self._locked_process():
            card, note, deck, preset, global_state = self._process.warm_up_state(
                _review_input_row(review_input)
            )
        return RwkvReviewerStateSnapshot(
            card_state=card,
            note_state=note,
            deck_state=deck,
            preset_state=preset,
            global_state=global_state,
        )

    def warm_up_reviews_in_place(
        self,
        reviews: Sequence[RwkvReviewInput],
    ) -> None:
        """Advance resident historical state without serializing a snapshot."""

        total = len(reviews)
        if total == 0:
            return
        backend_chunk_size = _rust_warmup_chunk_size(
            total,
            record_predictions=False,
        )
        warm_up_packed = getattr(self._process, "warm_up_reviews_packed", None)
        processed = 0
        with self._locked_process():
            while processed < total:
                chunk = reviews[processed : processed + backend_chunk_size]
                if callable(warm_up_packed):
                    warm_up_packed(_packed_warm_up_reviews(chunk), False)
                else:
                    self._process.warm_up_reviews(
                        [_review_input_row(review_input) for review_input in chunk],
                        False,
                    )
                processed += len(chunk)

    def warm_up_packed_rows_in_place(self, rows: bytes, count: int) -> None:
        """`warm_up_reviews_in_place` for `count` reviews already packed
        (`_packed_review_input_row`'s rows, one after another): the same
        chunks, the same calls."""

        if count == 0:
            return
        backend_chunk_size = _rust_warmup_chunk_size(
            count,
            record_predictions=False,
        )
        width = _PACKED_PREDICTION_REQUEST_ROW.size
        processed = 0
        with self._locked_process():
            while processed < count:
                chunk = min(backend_chunk_size, count - processed)
                self._process.warm_up_reviews_packed(
                    _PACKED_PREDICTION_REQUEST_HEADER.pack(
                        _PACKED_WARM_UP_REVIEW_MAGIC, chunk
                    )
                    + rows[processed * width : (processed + chunk) * width],
                    False,
                )
                processed += chunk

    def reset_warm_up_state(self) -> None:
        reset = getattr(self._process, "reset_warm_up_state", None)
        if callable(reset):
            with self._locked_process():
                reset()

    def predict_many(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        predict_many = getattr(self._process, "predict_many", None)
        if not callable(predict_many):
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

        build_start = time.monotonic()
        rows = [
            (
                request.review_input.identity.card_id,
                request.review_input.identity.note_id,
                request.review_input.identity.deck_id,
                request.review_input.identity.preset_id,
                request.review_input.is_query,
                request.review_input.ease,
                request.review_input.duration_millis,
                request.review_input.card_type,
                request.review_input.day_offset,
                request.review_input.current_elapsed_days,
                request.review_input.current_elapsed_seconds,
                *request.review_input.target_retentions,
                request.review_input.enforce_grade_order,
                _state_bytes(request.card_state),
                _state_bytes(request.note_state),
                _state_bytes(request.deck_state),
                _state_bytes(request.preset_state),
                _state_bytes(request.global_state),
            )
            for request in requests
        ]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust batch bridge started: requests=%s build_elapsed_ms=%.1f",
            len(requests),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(requests):
            raise ValueError("RWKV Rust batch prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust batch predicted: requests=%s "
            "build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(requests),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )

        return [
            RwkvReviewPrediction(
                retrievability=float(retrievability),
                curve_retrievability=(
                    float(curve_retrievability)
                    if curve_retrievability is not None
                    else None
                ),
                current_interval=_optional_interval(current_interval),
                current_interval_unrounded=_optional_unrounded_interval(
                    current_interval_unrounded
                ),
                current_s90=_optional_unrounded_interval(current_s90),
                interval_overrides=_unrounded_interval_override_from_tuple(intervals),
                s90_overrides=_unrounded_interval_override_from_tuple(s90s),
                button_probabilities=_button_probabilities_from_tuple(
                    button_probabilities
                ),
            )
            for (
                retrievability,
                curve_retrievability,
                current_interval,
                current_s90,
                intervals,
                s90s,
                button_probabilities,
                current_interval_unrounded,
            ) in outputs
        ]

    def predict_retrievability_many(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[float]:
        predict_many_packed = getattr(
            self._process,
            "predict_retrievability_many_packed",
            None,
        )
        predict_many_tuple = getattr(self._process, "predict_retrievability_many", None)
        if not callable(predict_many_packed) and not callable(predict_many_tuple):
            return [
                float(prediction.retrievability)
                if prediction is not None and prediction.retrievability is not None
                else float("nan")
                for prediction in self.predict_many(requests)
            ]

        build_start = time.monotonic()
        use_packed = not callable(predict_many_tuple) and callable(predict_many_packed)
        payload = (
            _packed_prediction_requests(requests)
            if use_packed
            else [_prediction_request_row(request) for request in requests]
        )
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust retrievability batch bridge started: "
            "requests=%s packed=%s build_elapsed_ms=%.1f",
            len(requests),
            use_packed,
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = (
                predict_many_packed(*payload)
                if use_packed
                else predict_many_tuple(payload)
            )
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(requests):
            raise ValueError("RWKV Rust retrievability prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust retrievability batch predicted: requests=%s "
            "packed=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(requests),
            use_packed,
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )

        return [float(retrievability) for retrievability in outputs]

    def predict_retrievability_many_from_warm_up(
        self,
        review_inputs: Sequence[RwkvReviewInput],
    ) -> Sequence[float]:
        predict_many = getattr(
            self._process,
            "predict_retrievability_many_from_warm_up",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV resident-state prediction is unavailable")

        build_start = time.monotonic()
        rows = [_review_input_row(review_input) for review_input in review_inputs]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust resident retrievability batch started: "
            "requests=%s build_elapsed_ms=%.1f",
            len(rows),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(review_inputs):
            raise ValueError("RWKV Rust resident prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust resident retrievability batch predicted: "
            "requests=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f "
            "elapsed_ms=%.1f",
            len(rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [float(retrievability) for retrievability in outputs]

    def predict_current_intervals_many_from_warm_up(
        self,
        review_inputs: Sequence[RwkvReviewInput],
    ) -> Sequence[tuple[float | None, int | None, float | None, float | None]]:
        """The stored curve's recall now, current interval, S90 and unrounded
        current interval per input, from the curve RWKV stored at the card's
        last answered review (spec sched.rwkv-curve-reschedule).

        No model pass, no state bytes across the bridge, GIL released in
        Rust. Used by "Reschedule cards with RWKV-Curve".
        """

        predict_many = getattr(
            self._process,
            "predict_current_intervals_many_from_warm_up",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV resident-state interval prediction is unavailable")

        build_start = time.monotonic()
        rows = [_review_input_row(review_input) for review_input in review_inputs]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        with self._locked_process():
            outputs = predict_many(rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(review_inputs):
            raise ValueError("RWKV Rust resident interval prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust resident interval batch predicted: requests=%s "
            "build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [
            (
                float(curve_retrievability) if curve_retrievability else None,
                int(current_interval) if current_interval else None,
                float(current_s90) if current_s90 else None,
                float(unrounded) if unrounded else None,
            )
            for curve_retrievability, current_interval, current_s90, unrounded in outputs
        ]

    def predict_curve_retrievability_many_from_warm_up(
        self,
        review_inputs: Sequence[RwkvReviewInput],
    ) -> Sequence[float | None]:
        """Query-only curve retrievability from the resident state.

        This is the one number the Stats Retrievability graph needs under
        RWKV-Curve. The full prediction path additionally runs the four
        simulated-answer passes and the current-interval crossing search,
        serializes each card's state across the bridge and hashes it; none of
        that changes this value. `None` means the card has no curve value.
        """

        predict_many = getattr(
            self._process,
            "predict_curve_retrievability_many_from_warm_up",
            None,
        )
        if not callable(predict_many):
            raise ValueError(
                "RWKV resident curve retrievability prediction is unavailable"
            )

        build_start = time.monotonic()
        rows = [_review_input_row(review_input) for review_input in review_inputs]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        with self._locked_process():
            outputs = predict_many(rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(review_inputs):
            raise ValueError(
                "RWKV Rust resident curve retrievability prediction count mismatch"
            )

        logger.debug(
            "RWKV embedded Rust resident curve retrievability batch predicted: "
            "requests=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f "
            "elapsed_ms=%.1f",
            len(rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [
            None if curve_retrievability is None else float(curve_retrievability)
            for curve_retrievability in outputs
        ]

    def predict_memorised_retrievability_from_warm_up(
        self,
        review_inputs: Sequence[RwkvReviewInput],
        *,
        day: int,
    ) -> bytes:
        """Predict one Memorised day with packed inputs and packed f32 output."""

        predict_many = getattr(
            self._process,
            "predict_retrievability_many_from_warm_up_packed",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV packed resident-state prediction is unavailable")

        payload = _packed_memorised_query_inputs(review_inputs, day=day)
        with self._locked_process():
            outputs = bytes(predict_many(payload))
        if len(outputs) != len(review_inputs) * 4:
            raise ValueError("RWKV packed resident prediction count mismatch")
        return outputs

    def predict_memorised_retrievability_on_day(
        self,
        rows: MemorisedDayRows,
        *,
        day: int,
    ) -> bytes:
        """Predict one Memorised day from rows packed once per rating.

        The rows carry each card's own review day, and the Rust side derives
        the day and the two elapsed fields from `day`. So a day costs one
        packed buffer of rows that are already packed, not a fresh pack of
        every card (spec ui.stats-total-knowledge).
        """

        predict_many = getattr(
            self._process,
            "predict_retrievability_many_from_warm_up_packed_on_day",
            None,
        )
        if not callable(predict_many):
            raise ValueError(
                "RWKV packed resident-state prediction by day is unavailable"
            )

        payload = rows.payload()
        with self._locked_process():
            outputs = bytes(predict_many(payload, day))
        if len(outputs) != len(rows) * 4:
            raise ValueError("RWKV packed resident prediction count mismatch")
        return outputs

    def curve_retrievability_day_sums_from_warm_up(
        self,
        spans: Sequence[tuple[int, int, int, int]],
    ) -> tuple[int, Sequence[float]]:
        """Total Knowledge under RWKV-Curve (spec ui.stats-total-knowledge):
        `spans` are (card id, day of its last review, first day, last day);
        returns the first day and the per-day sums of the stored curves'
        recall from it."""

        with self._locked_process():
            first_day, sums = self._process.curve_retrievability_day_sums_from_warm_up(
                list(spans)
            )
        return int(first_day), sums

    def predict_retrievability_many_after_review(
        self,
        *,
        answer: RwkvReviewInput,
        query_inputs: Sequence[RwkvReviewInput],
        snapshot: RwkvBackendCacheSnapshot,
    ) -> Sequence[float]:
        predict_many = getattr(
            self._process,
            "predict_retrievability_many_after_review",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV future retrievability prediction is unavailable")

        build_start = time.monotonic()
        answer_row = _review_input_row(answer)
        query_rows = [_review_input_row(review_input) for review_input in query_inputs]
        snapshot_row = _workload_snapshot_for_review_inputs(
            snapshot,
            (answer, *query_inputs),
        )
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust future retrievability batch bridge started: "
            "requests=%s build_elapsed_ms=%.1f",
            len(query_rows),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(answer_row, query_rows, snapshot_row)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(query_inputs):
            raise ValueError("RWKV future retrievability prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust future retrievability batch predicted: "
            "requests=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(query_rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [float(retrievability) for retrievability in outputs]

    def predict_retrievability_many_after_reviews(
        self,
        *,
        answers: Sequence[RwkvReviewInput],
        query_inputs: Sequence[RwkvReviewInput],
        snapshot: RwkvBackendCacheSnapshot,
    ) -> Sequence[Sequence[float]]:
        predict_many = getattr(
            self._process,
            "predict_retrievability_many_after_reviews",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV future retrievability prediction is unavailable")

        build_start = time.monotonic()
        answer_rows = [_review_input_row(answer) for answer in answers]
        query_rows = [_review_input_row(review_input) for review_input in query_inputs]
        snapshot_row = _workload_snapshot_for_review_inputs(
            snapshot,
            (*answers, *query_inputs),
        )
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust future retrievability multi-answer batch bridge "
            "started: answers=%s requests=%s build_elapsed_ms=%.1f",
            len(answer_rows),
            len(query_rows),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(answer_rows, query_rows, snapshot_row)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(answers):
            raise ValueError("RWKV future retrievability answer count mismatch")
        if any(len(answer_outputs) != len(query_inputs) for answer_outputs in outputs):
            raise ValueError("RWKV future retrievability prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust future retrievability multi-answer batch predicted: "
            "answers=%s requests=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f "
            "elapsed_ms=%.1f",
            len(answer_rows),
            len(query_rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [
            [float(retrievability) for retrievability in answer_outputs]
            for answer_outputs in outputs
        ]

    def predict_retrievability_many_after_reviews_from_warm_up(
        self,
        *,
        answers: Sequence[RwkvReviewInput],
        query_inputs: Sequence[RwkvReviewInput],
    ) -> Sequence[Sequence[float]]:
        predict_many = getattr(
            self._process,
            "predict_retrievability_many_after_reviews_from_warm_up",
            None,
        )
        if not callable(predict_many):
            raise ValueError(
                "RWKV resident future retrievability prediction is unavailable"
            )

        build_start = time.monotonic()
        answer_rows = [_review_input_row(answer) for answer in answers]
        query_rows = [_review_input_row(review_input) for review_input in query_inputs]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust resident future retrievability multi-answer batch "
            "started: answers=%s requests=%s build_elapsed_ms=%.1f",
            len(answer_rows),
            len(query_rows),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(answer_rows, query_rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(answers):
            raise ValueError("RWKV resident future prediction answer count mismatch")
        if any(len(answer_outputs) != len(query_inputs) for answer_outputs in outputs):
            raise ValueError("RWKV resident future prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust resident future retrievability multi-answer batch "
            "predicted: answers=%s requests=%s build_elapsed_ms=%.1f "
            "bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(answer_rows),
            len(query_rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [
            [float(retrievability) for retrievability in answer_outputs]
            for answer_outputs in outputs
        ]

    def snapshot(self, review_input: RwkvReviewInput) -> object:
        with self._locked_process():
            return self._process.state_for_card(review_input.identity.card_id)

    def restore(self, state: object | None) -> None:
        if state is not None:
            with self._locked_process():
                self._process.restore_state(state)

    def forget_card(self, card_id: int) -> bool:
        """Forgets the card's own counters and stored curve, as if the state
        had never seen it (spec sched.rwkv-live-learning-start-fresh). True
        when the state had seen the card."""
        forget = getattr(self._process, "forget_card", None)
        if not callable(forget):
            return False
        with self._locked_process():
            return bool(forget(card_id))

    def card_curve(
        self, card_id: int, elapsed_days: Sequence[float]
    ) -> tuple[list[float], float] | None:
        """The card's stored RWKV-Curve curve at `elapsed_days` and its S90."""
        with self._locked_process():
            return self._process.card_curve(card_id, list(elapsed_days))

    def record_curve_sources(self, on: bool) -> None:
        """Starts or stops recording each answered review's curve source."""
        record = getattr(self._process, "record_curve_sources", None)
        if callable(record):
            with self._locked_process():
                record(on)

    def take_curve_sources(self) -> tuple[list[int], bytes, int] | None:
        """(each review's index in its call, the sources, bytes per source)
        recorded since the last call."""
        take = getattr(self._process, "take_curve_sources", None)
        if not callable(take):
            return None
        with self._locked_process():
            packed_indices, sources, width = take()
        return _unpack_u32s(packed_indices), bytes(sources), int(width)

    def curve_source_tag(self) -> tuple[int, int] | None:
        """(format, kernel) of this model's curve sources."""
        cached = getattr(self, "_curve_source_tag", None)
        return cached if cached is not None else self._read_curve_source_tag()

    def _read_curve_source_tag(self) -> tuple[int, int] | None:
        tag = getattr(self._process, "curve_source_tag", None)
        if not callable(tag):
            return None
        source_format, kernel, _width = tag()
        return int(source_format), int(kernel)

    def curves_from_sources(
        self,
        sources: RwkvCurveSources,
        elapsed_days: Sequence[float],
    ) -> list[tuple[list[float], float] | None] | None:
        """The curves rebuilt from saved `sources` (spec
        ui.card-info-rwkv-curve): for each, (recall at `elapsed_days`, S90)
        or None; None when they are not this model's."""
        rebuild = getattr(self._process, "curves_from_sources", None)
        if not callable(rebuild):
            return None
        with self._locked_process():
            return rebuild(
                sources.format,
                sources.kernel,
                sources.sources,
                sources.width,
                list(elapsed_days),
            )

    def card_curve_s90s(self, card_ids: bytes) -> bytes:
        """The S90 of each card's stored curve, packed as little-endian f32s
        (NaN without a curve), for `card_ids` packed as little-endian i64s."""
        with self._locked_process():
            return bytes(self._process.card_curve_s90s(card_ids))

    def card_curve_weights(self, card_ids: Sequence[int]) -> tuple[list[int], bytes]:
        """The stored RWKV-Curve curves of `card_ids` that have one, packed."""
        with self._locked_process():
            ids, curves = self._process.card_curve_weights(list(card_ids))
            return list(ids), bytes(curves)

    def cache_state(self) -> bytes:
        with self._locked_process():
            return bytes(self._process.cache_state())

    def restore_cache_state(self, state: bytes) -> None:
        with self._locked_process():
            self._process.restore_cache_state(state)

    def restore_warm_up_snapshot(self, snapshot: RwkvBackendCacheSnapshot) -> None:
        restore = getattr(self._process, "restore_warm_up_snapshot", None)
        if not callable(restore):
            raise ValueError("RWKV resident-state snapshot restore is unavailable")
        with self._locked_process():
            restore(_workload_snapshot(snapshot))

    def restore_warm_up_state(
        self,
        identity: RwkvReviewIdentity,
        snapshot: RwkvReviewerStateSnapshot,
    ) -> None:
        restore = getattr(self._process, "restore_warm_up_state", None)
        if not callable(restore):
            raise ValueError("RWKV resident-state restore is unavailable")
        with self._locked_process():
            restore(
                identity.card_id,
                identity.note_id,
                identity.deck_id,
                identity.preset_id,
                _state_bytes(snapshot.card_state),
                _state_bytes(snapshot.note_state),
                _state_bytes(snapshot.deck_state),
                _state_bytes(snapshot.preset_state),
                _state_bytes(snapshot.global_state),
            )


def _review_input_row(
    review_input: RwkvReviewInput,
) -> tuple[
    int,
    int | None,
    int | None,
    int | None,
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bool,
]:
    identity = review_input.identity
    return (
        identity.card_id,
        identity.note_id,
        identity.deck_id,
        identity.preset_id,
        review_input.is_query,
        review_input.ease,
        review_input.duration_millis,
        review_input.card_type,
        review_input.day_offset,
        review_input.current_elapsed_days,
        review_input.current_elapsed_seconds,
        *review_input.target_retentions,
        review_input.enforce_grade_order,
    )


def _workload_snapshot(
    snapshot: RwkvBackendCacheSnapshot,
) -> tuple[
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    bytes | None,
    bytes | None,
]:
    return (
        sorted(snapshot.card_states.items()),
        sorted(snapshot.note_states.items()),
        sorted(snapshot.deck_states.items()),
        sorted(snapshot.preset_states.items()),
        snapshot.global_state,
        snapshot.runtime_state,
    )


def _workload_snapshot_for_review_inputs(
    snapshot: RwkvBackendCacheSnapshot,
    review_inputs: Sequence[RwkvReviewInput],
) -> tuple[
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    bytes | None,
    bytes | None,
]:
    card_ids = {review_input.identity.card_id for review_input in review_inputs}
    note_ids = {
        note_id
        for review_input in review_inputs
        if (note_id := review_input.identity.note_id) is not None
    }
    deck_ids = {
        deck_id
        for review_input in review_inputs
        if (deck_id := review_input.identity.deck_id) is not None
    }
    preset_ids = {
        preset_id
        for review_input in review_inputs
        if (preset_id := review_input.identity.preset_id) is not None
    }
    return (
        _selected_state_items(snapshot.card_states, card_ids),
        _selected_state_items(snapshot.note_states, note_ids),
        _selected_state_items(snapshot.deck_states, deck_ids),
        _selected_state_items(snapshot.preset_states, preset_ids),
        snapshot.global_state,
        snapshot.runtime_state,
    )


def _selected_state_items(
    states: dict[int, bytes],
    selected_ids: set[int],
) -> list[tuple[int, bytes]]:
    return [
        (state_id, states[state_id])
        for state_id in sorted(selected_ids)
        if state_id in states
    ]


def _unpack_u32s(packed: bytes) -> list[int]:
    values = array.array("I")
    values.frombytes(packed)
    if sys.byteorder == "big":
        values.byteswap()
    return values.tolist()


def _recorded_curve_sources(
    process: Any,
    review_ids: Sequence[int],
    processed: int,
) -> RwkvCurveSources | None:
    """The curve sources the runtime recorded during the last chunk, with
    each chunk index turned into its review id."""
    packed_indices, sources, width = process.take_curve_sources()
    indices = _unpack_u32s(packed_indices)
    if not indices:
        return None
    source_format, kernel, _width = process.curve_source_tag()
    return RwkvCurveSources(
        review_ids=[review_ids[processed + index] for index in indices],
        sources=bytes(sources),
        width=int(width),
        format=int(source_format),
        kernel=int(kernel),
    )


def _record_warm_up_predictions(
    prediction_recorder: Callable[[int, float], None],
    review_ids: Sequence[int],
    processed: int,
    predictions: Sequence[tuple[int, float, float | None]],
    *,
    curve_recorder: Callable[[int, float], None] | None = None,
) -> None:
    """Hands each recorder its OWN algorithm's value.

    The replay reports two numbers per review: RWKV-Instant's rating head,
    and RWKV-Curve's value, which is the curve the replay had stored at the
    card's previous answered review evaluated at this review's elapsed time.
    A card's first review has no such curve, and gets no curve row rather
    than a substitute (spec ui.stats-model-metrics).
    """
    rows = []
    curve_rows = []
    for prediction in predictions:
        index, retrievability = prediction[0], prediction[1]
        curve = prediction[2] if len(prediction) > 2 else None
        review_index = processed + int(index)
        if not 0 <= review_index < len(review_ids):
            continue
        review_id = review_ids[review_index]
        rows.append((review_id, float(retrievability)))
        if curve is not None and math.isfinite(curve):
            curve_rows.append((review_id, float(curve)))

    _hand_to_recorder(prediction_recorder, rows)
    if curve_recorder is not None:
        _hand_to_recorder(curve_recorder, curve_rows)


def _hand_to_recorder(
    recorder: Callable[[int, float], None],
    rows: Sequence[tuple[int, float]],
) -> None:
    if not rows:
        return
    record_many = getattr(recorder, "record_many", None)
    if callable(record_many):
        record_many(rows)
    else:
        for review_id, prediction in rows:
            recorder(review_id, prediction)


def _prediction_request_row(
    request: RwkvReviewPredictionRequest,
) -> tuple[
    int,
    int | None,
    int | None,
    int | None,
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bool,
    bytes | None,
    bytes | None,
    bytes | None,
    bytes | None,
    bytes | None,
]:
    return (
        *_review_input_row(request.review_input),
        _state_bytes(request.card_state),
        _state_bytes(request.note_state),
        _state_bytes(request.deck_state),
        _state_bytes(request.preset_state),
        _state_bytes(request.global_state),
    )


def _packed_prediction_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
) -> tuple[
    bytes,
    tuple[
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
    ],
]:
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_PREDICTION_REQUEST_MAGIC,
            len(requests),
        )
    )
    card_states: list[bytes | None] = []
    note_states: list[bytes | None] = []
    deck_states: list[bytes | None] = []
    preset_states: list[bytes | None] = []
    global_states: list[bytes | None] = []

    for request in requests:
        payload.extend(_packed_review_input_row(request.review_input))

        card_states.append(_state_bytes(request.card_state))
        note_states.append(_state_bytes(request.note_state))
        deck_states.append(_state_bytes(request.deck_state))
        preset_states.append(_state_bytes(request.preset_state))
        global_states.append(_state_bytes(request.global_state))

    return (
        bytes(payload),
        (card_states, note_states, deck_states, preset_states, global_states),
    )


def _packed_review_input_row(
    review_input: RwkvReviewInput,
    *,
    query_day: int | None = None,
) -> bytes:
    identity = review_input.identity
    presence = 0

    def optional_i64(value: int | None, bit: int) -> int:
        nonlocal presence
        if value is None:
            return 0
        presence |= 1 << bit
        return int(value)

    def optional_f32(value: float | None, bit: int) -> float:
        nonlocal presence
        if value is None:
            return 0.0
        presence |= 1 << bit
        return float(value)

    note_id = optional_i64(identity.note_id, 0)
    deck_id = optional_i64(identity.deck_id, 1)
    preset_id = optional_i64(identity.preset_id, 2)
    if query_day is None:
        is_query = review_input.is_query
        ease_value = review_input.ease
        duration_millis_value = review_input.duration_millis
        day_offset_value = review_input.day_offset
        current_elapsed_days_value = review_input.current_elapsed_days
        current_elapsed_seconds_value = review_input.current_elapsed_seconds
    else:
        if review_input.day_offset is None:
            raise ValueError("RWKV Memorised query input has no review day")
        elapsed_days = max(0, query_day - review_input.day_offset)
        is_query = True
        ease_value = None
        duration_millis_value = None
        day_offset_value = query_day
        current_elapsed_days_value = elapsed_days
        current_elapsed_seconds_value = elapsed_days * 86_400

    ease = optional_i64(ease_value, 3)
    duration_millis = optional_i64(duration_millis_value, 4)
    card_type = optional_i64(review_input.card_type, 5)
    day_offset = optional_i64(day_offset_value, 6)
    current_elapsed_days = optional_i64(current_elapsed_days_value, 7)
    current_elapsed_seconds = optional_i64(current_elapsed_seconds_value, 8)
    target_retention_again = optional_f32(review_input.target_retentions[0], 9)
    target_retention_hard = optional_f32(review_input.target_retentions[1], 10)
    target_retention_good = optional_f32(review_input.target_retentions[2], 11)
    target_retention_easy = optional_f32(review_input.target_retentions[3], 12)

    return _PACKED_PREDICTION_REQUEST_ROW.pack(
        presence,
        identity.card_id,
        note_id,
        deck_id,
        preset_id,
        1 if is_query else 0,
        ease,
        duration_millis,
        card_type,
        day_offset,
        current_elapsed_days,
        current_elapsed_seconds,
        target_retention_again,
        target_retention_hard,
        target_retention_good,
        target_retention_easy,
        1 if review_input.enforce_grade_order else 0,
    )


def _packed_warm_up_reviews(reviews: Sequence[RwkvReviewInput]) -> bytes:
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_WARM_UP_REVIEW_MAGIC,
            len(reviews),
        )
    )
    for review_input in reviews:
        payload.extend(_packed_review_input_row(review_input))
    return bytes(payload)


class MemorisedDayRows:
    """The rows of the cards a Memorised day scores.

    Total Knowledge walks the collection day by day and scores every card
    that has a rating and no later reset. A card's row changes only when the
    card is rated or reset. Three fields of the packed
    row change every day - the day and the two elapsed fields - and all three
    follow from the query day and the row's own review day, so the Rust side
    derives them. A row is therefore packed once per rating instead of once
    per day (spec ui.stats-total-knowledge).

    The order of the rows is the order a `dict` keyed by card gives: a card
    that is rated again keeps its place, and a card that is removed and rated
    again goes to the end. So the sum over the rows keeps its old order.
    """

    __slots__ = (
        "_rows",
        "_inputs",
        "_card_ids",
        "_index",
        "_unpacked",
        "_removed",
        "_prepacked",
    )

    def __init__(self) -> None:
        # a row is None when its card is gone, or when the input is new and
        # nothing has asked for the payload yet
        self._rows: list[bytes | None] = []
        self._inputs: list[RwkvReviewInput | None] = []
        self._card_ids: list[int | None] = []
        self._index: dict[int, int] = {}
        self._unpacked: list[int] = []
        self._removed = 0
        # a row came packed (`set_row`): it has no input to give back
        self._prepacked = False

    def __len__(self) -> int:
        return len(self._index)

    def set(self, card_id: int, review_input: RwkvReviewInput) -> None:
        """Make `review_input` the card's row, in its place if it has one."""

        position = self._index.get(card_id)
        if position is None:
            self._index[card_id] = len(self._rows)
            self._rows.append(None)
            self._inputs.append(review_input)
            self._card_ids.append(card_id)
            self._unpacked.append(len(self._rows) - 1)
        else:
            self._rows[position] = None
            self._inputs[position] = review_input
            self._unpacked.append(position)

    def set_row(self, card_id: int, row: bytes) -> None:
        """`set` for a rating that comes packed already
        (`_packed_review_input_row(review_input)`)."""

        self._prepacked = True
        position = self._index.get(card_id)
        if position is None:
            self._index[card_id] = len(self._rows)
            self._rows.append(row)
            self._inputs.append(None)
            self._card_ids.append(card_id)
        else:
            self._rows[position] = row
            self._inputs[position] = None

    def remove(self, card_id: int) -> None:
        """Drop the card's row. The later rows keep their places, and the
        empty places are cleared away once they reach a quarter of the rows,
        so a removal costs a constant amount of work over a run."""

        position = self._index.pop(card_id, None)
        if position is None:
            return
        self._rows[position] = None
        self._inputs[position] = None
        self._card_ids[position] = None
        self._removed += 1
        if self._removed * 4 >= len(self._rows):
            self._compact()

    def _pack_new_rows(self) -> None:
        for position in self._unpacked:
            review_input = self._inputs[position]
            # None here means the card was removed after it was set
            if review_input is not None:
                self._rows[position] = _packed_review_input_row(review_input)
        self._unpacked.clear()

    def _compact(self) -> None:
        """Close the empty places up, keeping the order. A row that is not
        packed yet stays unpacked: compaction must not pack, because it can
        run while the caller is still adding the day's ratings."""

        new_position: dict[int, int] = {}
        rows: list[bytes | None] = []
        inputs: list[RwkvReviewInput | None] = []
        card_ids: list[int | None] = []
        for position, card_id in enumerate(self._card_ids):
            if card_id is None:
                continue
            new_position[position] = len(card_ids)
            card_ids.append(card_id)
            rows.append(self._rows[position])
            inputs.append(self._inputs[position])
        self._card_ids = card_ids
        self._rows = rows
        self._inputs = inputs
        self._index = {
            card_id: position
            for position, card_id in enumerate(card_ids)
            if card_id is not None
        }
        self._unpacked = [
            new_position[position]
            for position in self._unpacked
            if position in new_position
        ]
        self._removed = 0

    def card_ids(self) -> list[int]:
        """The cards, in the order their predictions come back."""

        return [card_id for card_id in self._card_ids if card_id is not None]

    def review_inputs(self) -> list[RwkvReviewInput]:
        """The cards' rating inputs, in the order `card_ids` gives."""

        if self._prepacked:
            raise ValueError("a row set packed has no rating input")
        return [
            review_input for review_input in self._inputs if review_input is not None
        ]

    def payload(self) -> bytes:
        """The packed rows, for `predict_memorised_retrievability_on_day`."""

        self._pack_new_rows()
        # a live row is 1 or more bytes, so only an empty place is dropped
        rows = list(filter(None, self._rows))
        return _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_WARM_UP_REVIEW_MAGIC,
            len(rows),
        ) + b"".join(rows)


def _packed_memorised_query_inputs(
    review_inputs: Sequence[RwkvReviewInput],
    *,
    day: int,
) -> bytes:
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_WARM_UP_REVIEW_MAGIC,
            len(review_inputs),
        )
    )
    for review_input in review_inputs:
        payload.extend(_packed_review_input_row(review_input, query_day=day))
    return bytes(payload)


def _warmup_progress_interval(total: int) -> int:
    if total <= 0:
        return 1
    return max(1, min(1000, total // 100 or 1))


def _rust_warmup_chunk_size(
    total: int,
    *,
    record_predictions: bool = False,
    batch_rows: int | None = None,
) -> int:
    if batch_rows is not None:
        # the caller replays in short batches, so that it can step aside for
        # the user between two of them
        return max(1, min(total, batch_rows))
    if not record_predictions:
        return max(1, min(total, _RUST_STATE_ONLY_WARMUP_CHUNK_SIZE))

    progress_interval = _warmup_progress_interval(total)
    if total <= _RUST_WARMUP_CHUNK_SIZE:
        return progress_interval
    return max(progress_interval, _RUST_WARMUP_CHUNK_SIZE)


def _report_warmup_progress(
    progress: RwkvWarmUpProgressCallback | None,
    *,
    processed: int,
    total: int,
) -> None:
    if progress is not None:
        progress(RwkvWarmUpProgress(processed_reviews=processed, total_reviews=total))


def _state_bytes(state: object | None) -> bytes | None:
    if state is None:
        return None
    if isinstance(state, bytes):
        return state
    raise TypeError("RWKV Rust state must be bytes")


def _whole_interval(value: float | None) -> int | None:
    """A (possibly unrounded) interval in whole days, rounded up, at least 1;
    the current interval stays whole days (the S90 does not, spec
    sched.rwkv-curve-s90)."""

    return None if value is None else max(1, math.ceil(value))


def _unrounded_interval_override_from_tuple(values: object) -> RwkvIntervalOverride:
    """Answer intervals and S90s from the embedded runtime: unrounded days
    (spec sched.sub-day-intervals, sched.rwkv-curve-s90)."""

    if not isinstance(values, tuple) or len(values) != 4:
        return RwkvIntervalOverride()

    return RwkvIntervalOverride(
        again=_optional_unrounded_interval(values[0]),
        hard=_optional_unrounded_interval(values[1]),
        good=_optional_unrounded_interval(values[2]),
        easy=_optional_unrounded_interval(values[3]),
    )


def _optional_unrounded_interval(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _button_probabilities_from_tuple(
    values: object,
) -> RwkvButtonProbabilities | None:
    if not isinstance(values, tuple) or len(values) != 4:
        return None

    probabilities = []
    for value in values:
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        probabilities.append(float(value))

    return cast(RwkvButtonProbabilities, tuple(probabilities))


def _optional_interval(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class SrsBenchmarkReviewRowBuilder:
    def __init__(self, row_factory: Callable[[dict[str, object]], object]) -> None:
        self._row_factory = row_factory

    def row_for(self, review_input: RwkvReviewInput) -> object:
        identity = review_input.identity
        elapsed_seconds = _elapsed_seconds(review_input)
        elapsed_days = _elapsed_days(review_input, elapsed_seconds)

        return self._row_factory(
            {
                "card_id": identity.card_id,
                "note_id": identity.note_id,
                "deck_id": identity.deck_id,
                "preset_id": identity.preset_id,
                "elapsed_days": elapsed_days,
                "elapsed_seconds": elapsed_seconds,
                "day_offset": review_input.day_offset or 0,
                "duration": _duration_millis(review_input),
                "state": review_input.card_type or 0,
                "rating": review_input.ease or 1,
            }
        )


def _load_srs_benchmark_process(
    *,
    benchmark_path: Path,
    model_path: Path,
    device: str,
    dtype: str,
) -> tuple[object, Callable[[dict[str, object]], object]]:
    sys.path.insert(0, str(benchmark_path))
    _install_srs_benchmark_import_shims()
    import pandas as pd  # type: ignore[import-untyped, import-not-found]
    import torch  # type: ignore[import-not-found]
    from rwkv.run_as_rnn import RNNProcess  # type: ignore[import-not-found]

    torch_dtype = _torch_dtype(torch, dtype)
    process = RNNProcess(
        path=model_path,
        device=torch.device(device),
        dtype=torch_dtype,
    )
    process.rnn.eval()
    return (
        _SrsBenchmarkProcessAdapter(process, torch),
        lambda row: pd.Series(row, dtype="float64"),
    )


class _SrsBenchmarkProcessAdapter:
    """Keep the optional upstream runner within Anki's inference contract."""

    def __init__(self, process: object, torch: Any) -> None:
        self._process: Any = process
        self._torch = torch

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)

    def predict_func(self, curve: tuple[Any, Any], elapsed_seconds: int) -> Any:
        elapsed_seconds_tensor = self._torch.tensor(
            elapsed_seconds,
            device=self._process.device,
        ).view(1, 1)
        out_ahead_logits, out_w = curve
        curve_probs_raw = self._process.rnn.forgetting_curve(
            out_w, elapsed_seconds_tensor
        )
        curve_logits_raw = self._torch.log(curve_probs_raw / (1 - curve_probs_raw))
        ahead_logit_residual = _safe_srs_benchmark_interp(
            self._torch,
            self._process.rnn,
            out_ahead_logits,
            elapsed_seconds_tensor,
        )
        return self._torch.sigmoid(curve_logits_raw + ahead_logit_residual)


def _safe_srs_benchmark_interp(
    torch: Any,
    rnn: Any,
    out_ahead_logits: Any,
    elapsed_seconds: Any,
) -> Any:
    elapsed_seconds = torch.clamp(elapsed_seconds.contiguous(), min=1)
    point_space_raw = torch.exp(
        torch.linspace(
            0,
            rnn.point_spread,
            rnn.num_points,
            device=out_ahead_logits.device,
        )
    )
    point_space = 0.5 + (point_space_raw - 1) * (
        math.e ** (rnn.max_e - rnn.point_spread)
    )
    # The upstream interpolation indexes past this grid for Anki's 36,500-day
    # maximum. Clamp only the ahead residual; the forgetting curve still uses
    # the actual elapsed time.
    interpolation_elapsed_seconds = torch.minimum(elapsed_seconds, point_space[-1])
    right_idx = torch.searchsorted(point_space, interpolation_elapsed_seconds)
    right_idx = torch.clamp(right_idx, min=1, max=rnn.num_points - 1)
    left_idx = right_idx - 1
    xl, xr = point_space[left_idx], point_space[right_idx]
    yl = torch.gather(out_ahead_logits, dim=-1, index=left_idx)
    yr = torch.gather(out_ahead_logits, dim=-1, index=right_idx)
    result = 1e-5 + (1 - 2 * 1e-5) * (
        yl + (yr - yl) * (interpolation_elapsed_seconds - xl) / (xr - xl)
    )
    return result.squeeze(-1)


def _torch_dtype(torch: Any, dtype: str) -> Any:
    return {
        "float": torch.float32,
        "float32": torch.float32,
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
    }[dtype]


def _install_srs_benchmark_import_shims() -> None:
    if not _module_available("tomli"):
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            pass
        else:
            sys.modules["tomli"] = tomllib
    if not _module_available("lmdb"):
        sys.modules["lmdb"] = types.ModuleType("lmdb")


def _module_available(module_name: str) -> bool:
    return (
        module_name in sys.modules or importlib.util.find_spec(module_name) is not None
    )


def _elapsed_seconds(review_input: RwkvReviewInput) -> int:
    if review_input.current_elapsed_seconds is not None:
        return review_input.current_elapsed_seconds
    if review_input.current_elapsed_days is not None:
        return review_input.current_elapsed_days * 86_400
    return -1


def _elapsed_days(review_input: RwkvReviewInput, elapsed_seconds: int) -> int:
    if review_input.current_elapsed_days is not None:
        return review_input.current_elapsed_days
    if elapsed_seconds >= 0:
        return elapsed_seconds // 86_400
    return -1


def _duration_millis(review_input: RwkvReviewInput) -> float:
    if review_input.duration_millis is None:
        return 0.0
    return float(review_input.duration_millis)


# Points (days) searched inside the first day for unrounded answer intervals;
# the same grid as SUB_DAY_SEARCH_POINTS in rslib/src/rwkv/mod.rs.
_SUB_DAY_SEARCH_POINTS = (
    1 / 1440,
    5 / 1440,
    10 / 1440,
    20 / 1440,
    30 / 1440,
    1 / 24,
    2 / 24,
    3 / 24,
    4 / 24,
    6 / 24,
    8 / 24,
    12 / 24,
    16 / 24,
    20 / 24,
)


def _interval_search_days(max_interval_days: int) -> list[int]:
    if max_interval_days <= 30:
        return list(range(1, max_interval_days + 1))

    days = list(range(1, 31))
    day = 45
    while day < max_interval_days:
        days.append(day)
        day = int(day * 1.5)
    days.append(max_interval_days)
    return days


def _probability_as_float(probability: object) -> float:
    detach = getattr(probability, "detach", None)
    if callable(detach):
        probability = detach()

    cpu = getattr(probability, "cpu", None)
    if callable(cpu):
        probability = cpu()

    item = getattr(probability, "item", None)
    if callable(item):
        return float(item())

    return float(cast(Any, probability))

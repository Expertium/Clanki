# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The guard that keeps the two replay SQL texts together.

`sched.rwkv-replay-start-row` is written twice: once in the backend query
(`rwkv_historical_review_rows`, `rslib/src/storage/revlog/mod.rs`) and once in
the Python replay query (`_historical_rwkv_review_rows`,
`qt/aqt/rwkv_scheduler.py`). The backend fingerprint is what stops the two from
drifting apart: it hashes the rows the backend reads and compares them with the
rows Python read, and a disagreement surfaces as `history_is_valid = false`.

A guard that cannot fail is not a guard, so this test breaks one clause of the
Python query on purpose and proves the fingerprint says so.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki.collection import Collection
from anki.decks import DeckId
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import RwkvHistoricalReviewInputs

# (ease, review kind, ease factor)
RATED_REVIEW = (3, 1, 2500)
RATED_RELEARNING = (1, 2, 2500)
FORGET = (0, 4, 0)

# The clause that carries the Forget cut, as `_historical_rwkv_review_rows`
# writes it. Removing it is the drift this test simulates.
FORGET_CUT_CLAUSE = "    and (f.forget_id is null or e.id > f.forget_id)\n"


def _add_forgotten_review_only_card(col: Collection) -> int:
    """A card with no Learning row and a Forget in the middle of its history.

    The same shape as the acceptance test in `test_rwkv_scheduler.py`: the
    replay must start it at the first rated row after the Forget.
    """
    # Raw rows, not the note API: the notetype names depend on the collection's
    # language, and the whole suite may have set a different one.
    card_id = 1_700_000_000_000
    col.db.execute(
        "insert into cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, "
        "factor, reps, lapses, left, odue, odid, flags, data) "
        "values (?, 1, ?, 0, 0, -1, 2, 2, 1, 20, 2500, 0, 0, 0, 0, 0, 0, '')",
        card_id,
        int(DeckId(1)),
    )

    day = 86_400 * 1000
    base = card_id + day
    for offset, (ease, kind, factor) in enumerate(
        (RATED_REVIEW, RATED_REVIEW, FORGET, RATED_REVIEW, RATED_RELEARNING)
    ):
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (?, ?, -1, ?, 10, 5, ?, 1000, ?)",
            base + offset * day,
            card_id,
            ease,
            factor,
            kind,
        )
    return card_id


def _fingerprint_accepts_the_python_history(reviewer: object) -> bool:
    """Whether the backend agrees with the rows the Python replay just read."""
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
        reviewer,
        expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=history.last_review_id,
            review_count=history.review_count,
            history_hash=history.history_hash,
        ),
    )
    assert fingerprint is not None, "the backend fingerprint is unavailable"
    return fingerprint.history_is_valid


class _DriftingDB:
    """`col.db` with one clause cut out of the replay query's SQL text.

    Everything else is forwarded to the real database, and the backend is
    untouched: it still reads the collection directly.
    """

    def __init__(self, db: Any, removed_clause: str) -> None:
        self._db = db
        self._removed_clause = removed_clause
        self.removed_it = False

    def all(self, sql: str, *args: object) -> list[Any]:
        if self._removed_clause in sql:
            sql = sql.replace(self._removed_clause, "")
            self.removed_it = True
        return self._db.all(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def test_replay_sql_that_drifts_from_the_backend_fails_the_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the Python query, which the backend's build of the whole history
    # would otherwise stand in for
    monkeypatch.setattr(
        rwkv_scheduler,
        "_backend_historical_rwkv_review_inputs",
        lambda *_args, **_kwargs: None,
    )
    col = Collection(str(tmp_path / "rwkv-replay-drift.anki2"))
    try:
        _add_forgotten_review_only_card(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        # The two texts agree today: the replay starts after the Forget, so it
        # reads two of the card's four rated rows, and the fingerprint accepts
        # the history.
        assert rwkv_scheduler._historical_rwkv_review_inputs(reviewer).review_count == 2
        assert _fingerprint_accepts_the_python_history(reviewer) is True

        # Now drop the Forget cut from the Python query only. The backend still
        # cuts, so it reads fewer rows than Python does.
        drifting = _DriftingDB(col.db, FORGET_CUT_CLAUSE)
        col.db = drifting

        undrifted = rwkv_scheduler._historical_rwkv_review_inputs(
            SimpleNamespace(mw=SimpleNamespace(col=col))
        )
        assert drifting.removed_it, (
            "the Forget-cut clause was not found in the replay SQL; "
            "update FORGET_CUT_CLAUSE to match the query"
        )
        # The broken query really does read more rows, so the drift is real and
        # the assertion below is not passing for some unrelated reason.
        assert undrifted.review_count == 4

        assert _fingerprint_accepts_the_python_history(reviewer) is False
    finally:
        col.close()


def _add_card_with_history(
    col: Collection,
    card_id: int,
    rows: list[tuple[int, int, int]],
    *,
    deleted: bool = False,
) -> None:
    """`deleted`: the card's row is gone and only its reviews are left."""
    if not deleted:
        col.db.execute(
            "insert into cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, "
            "factor, reps, lapses, left, odue, odid, flags, data) "
            "values (?, 1, ?, 0, 0, -1, 2, 2, 1, 20, 2500, 0, 0, 0, 0, 0, 0, '')",
            card_id,
            int(DeckId(1)),
        )
    day = 86_400 * 1000
    for offset, (ease, kind, factor) in enumerate(rows):
        # interleaved review times across cards, so a merge must really merge
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (?, ?, -1, ?, 10, 5, ?, 1000, ?)",
            1_600_000_000_000 + offset * day + card_id % 997,
            card_id,
            ease,
            factor,
            kind,
        )


def test_the_history_query_in_parts_reads_the_same_rows_and_can_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the query itself, not the backend's rows
    monkeypatch.setattr(
        rwkv_scheduler,
        "_backend_historical_rwkv_review_rows",
        lambda col, **_kwargs: None,
    )
    col = Collection(str(tmp_path / "rwkv-replay-parts.anki2"))
    try:
        learning = (3, 0, 2500)
        shapes = [
            [learning, RATED_REVIEW, RATED_RELEARNING, RATED_REVIEW],
            [RATED_REVIEW, RATED_REVIEW, FORGET, RATED_REVIEW, RATED_RELEARNING],
            [learning, learning, RATED_REVIEW, FORGET, learning, RATED_REVIEW],
            [RATED_REVIEW, RATED_RELEARNING],
        ]
        for n in range(25):
            _add_card_with_history(col, 1_700_000_000_000 + n * 7_919, shapes[n % 4])
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        whole = rwkv_scheduler._historical_rwkv_review_rows(reviewer)
        calls = []
        parts = rwkv_scheduler._historical_rwkv_review_rows(
            reviewer, between_parts=lambda: calls.append(1)
        )
        assert len(whole) > 50
        # the same rows, in the same order, value for value
        assert [tuple(row) for row in parts] == [tuple(row) for row in whole]
        ranges = rwkv_scheduler._card_id_ranges(col, rwkv_scheduler.HISTORY_QUERY_PARTS)
        # one step before each part, and at least one more for merging them
        # (spec sched.rwkv-recordings-automatic: every step is bounded, not
        # only the query)
        assert len(calls) > len(ranges) > 1

        # a caller that raises stops the query before its next part
        def stop() -> None:
            if calls:
                raise InterruptedError()
            calls.append(1)

        calls.clear()
        try:
            rwkv_scheduler._historical_rwkv_review_rows(reviewer, between_parts=stop)
        except InterruptedError:
            stopped = True
        else:
            stopped = False
        assert stopped and len(calls) == 1
    finally:
        col.close()


def test_the_backend_reads_the_same_whole_history_rows_as_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-history read takes its rows from the backend
    (`RwkvHistoricalReviewRows`): they must be the rows of the Python query,
    value for value and in the same order, Forget cuts and start rows
    included (spec sched.rwkv-replay-start-row)."""
    col = Collection(str(tmp_path / "rwkv-replay-backend.anki2"))
    try:
        _build_replay_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        query = rwkv_scheduler._historical_rwkv_review_rows(reviewer)
        backend = rwkv_scheduler._backend_historical_rwkv_review_rows(col)
        assert backend is not None, "the backend did not answer"
        assert len(query) > 50
        assert [tuple(row) for row in backend] == [tuple(row) for row in query]
        # the deleted cards' reviews are there, with no note and no deck
        deleted = [row for row in query if row[2] is None]
        assert len(deleted) > 5
        assert all(row[3] is None for row in deleted)
        # rows that cross the chunks the columns are read in are the same rows
        monkeypatch.setattr(rwkv_scheduler, "BACKEND_ROWS_CHUNK", 7)
        assert rwkv_scheduler._backend_historical_rwkv_review_rows(col) == backend
        # and the whole-history read with steps uses them
        calls: list[int] = []
        read = rwkv_scheduler._historical_rwkv_review_rows(
            reviewer, between_parts=lambda: calls.append(1)
        )
        assert read == backend
        assert len(calls) == 2

        # a caller that has stopped does not start the read
        def stop() -> None:
            raise InterruptedError()

        with pytest.raises(InterruptedError):
            rwkv_scheduler._historical_rwkv_review_rows(reviewer, between_parts=stop)
    finally:
        col.close()


def _build_replay_collection(col: Collection) -> None:
    learning = (3, 0, 2500)
    shapes = [
        [learning, RATED_REVIEW, RATED_RELEARNING, RATED_REVIEW],
        [RATED_REVIEW, RATED_REVIEW, FORGET, RATED_REVIEW, RATED_RELEARNING],
        [learning, learning, RATED_REVIEW, FORGET, learning, RATED_REVIEW],
        [RATED_REVIEW, RATED_RELEARNING],
    ]
    for n in range(25):
        # every sixth card is deleted: its reviews stay in the replay (spec
        # sched.rwkv-replay-deleted-cards)
        _add_card_with_history(
            col, 1_700_000_000_000 + n * 7_919, shapes[n % 4], deleted=n % 6 == 2
        )


def test_a_read_after_a_review_id_returns_the_same_rows_as_the_whole_read(
    tmp_path: Path,
) -> None:
    """The rows of a read after a review id, kept equal to the whole read.

    The query for a read after a review id scans only the cards that have a
    review that late. The rows it returns must still be exactly the rows the
    whole-history read returns after the same point, start rows included: a
    card's start row comes from its whole history, and the restriction is by
    card, never by review id.
    """
    col = Collection(str(tmp_path / "rwkv-replay-after.anki2"))
    try:
        _build_replay_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        whole = rwkv_scheduler._historical_rwkv_review_rows(reviewer)
        assert len(whole) > 50
        review_ids = sorted({int(row[0]) for row in whole})

        for cutoff in (
            review_ids[0],
            review_ids[len(review_ids) // 3],
            review_ids[len(review_ids) // 2],
            review_ids[-2],
        ):
            after = rwkv_scheduler._historical_rwkv_review_rows(
                reviewer, after_review_id=cutoff
            )
            expected = [row for row in whole if int(row[0]) > cutoff]
            assert [tuple(row) for row in after] == [tuple(row) for row in expected], (
                f"the read after {cutoff} disagrees with the whole read"
            )
            # the cut has to be doing something, or this proves nothing
            assert 0 < len(after) < len(whole)

        # the same, in parts, because the start-up read runs the query split
        split = rwkv_scheduler._historical_rwkv_review_rows(
            reviewer,
            after_review_id=review_ids[len(review_ids) // 2],
            between_parts=lambda: None,
        )
        expected = [
            row for row in whole if int(row[0]) > review_ids[len(review_ids) // 2]
        ]
        assert [tuple(row) for row in split] == [tuple(row) for row in expected]
    finally:
        col.close()


def test_an_incremental_read_matches_a_whole_read_of_the_enlarged_history(
    tmp_path: Path,
) -> None:
    """The result of an incremental read, kept equal to the whole read.

    This is the post-sync path: the state cache holds the history up to a
    review id, new reviews arrive, and only they are read. The result must be
    the whole history's result, value for value -- including `review_count`,
    which counts every review in the collection and not only the new ones.
    """
    col = Collection(str(tmp_path / "rwkv-replay-incremental.anki2"))
    try:
        _build_replay_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        before = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
        assert before.review_count > 50

        # reviews arrive for cards that already have a history, and for one
        # that does not, which is the shape a sync brings
        day = 86_400 * 1000
        for n, card_id in enumerate(
            (1_700_000_000_000, 1_700_000_000_000 + 7_919, 1_700_000_999_999)
        ):
            if card_id == 1_700_000_999_999:
                # the card itself, with no history: its only review is the
                # one below, which arrives after the cutoff
                _add_card_with_history(col, card_id, [])
            col.db.execute(
                "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, "
                "time, type) values (?, ?, -1, 3, 12, 10, 2500, 1000, 1)",
                before.last_review_id + (n + 1) * day,
                card_id,
            )

        after_whole = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
        incremental = rwkv_scheduler._historical_rwkv_review_inputs(
            reviewer,
            after_review_id=before.last_review_id,
            previous_review_id_by_card=dict(before.previous_review_id_by_card),
            previous_interval_days_by_card=dict(before.previous_interval_days_by_card),
            review_count_by_card=dict(before.review_count_by_card),
            previous_history_hash=before.history_hash,
            previous_replay_key=before.replay_key,
        )

        new_review_count = after_whole.review_count - before.review_count
        assert new_review_count > 0
        assert len(incremental.reviews) == new_review_count
        assert incremental.reviews == after_whole.reviews[-new_review_count:]
        assert incremental.review_ids == after_whole.review_ids[-new_review_count:]
        # the counts and the identity are of the whole collection, not of the
        # part that was read
        assert incremental.review_count == after_whole.review_count
        assert incremental.last_review_id == after_whole.last_review_id
        assert incremental.history_hash == after_whole.history_hash
        assert incremental.replay_key == after_whole.replay_key
        assert (
            incremental.previous_review_id_by_card
            == after_whole.previous_review_id_by_card
        )
        assert incremental.review_count_by_card == after_whole.review_count_by_card
    finally:
        col.close()


LEARNING = (3, 0, 2500)
# a card whose only Learning row is ignored: training never sees the row, so
# the card has no learning start and starts at its first rated row
IGNORED_START_CARD = 1_700_000_900_001
IGNORED_START_REVIEWS = (1_650_000_000_001, 1_650_000_000_011, 1_650_000_000_021)
# a card whose ignored review decides no start row: after its learning start,
# with no Learning row after it
IGNORED_PLAIN_CARD = 1_700_000_900_002
IGNORED_PLAIN_REVIEWS = (
    1_650_000_000_002,
    1_650_000_000_012,
    1_650_000_000_022,
    1_650_000_000_032,
)


def _add_card_with_reviews(
    col: Collection, card_id: int, reviews: list[tuple[int, tuple[int, int, int]]]
) -> None:
    _add_card_with_history(col, card_id, [])
    for review_id, (ease, kind, factor) in reviews:
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (?, ?, -1, ?, 10, 5, ?, 1000, ?)",
            review_id,
            card_id,
            ease,
            factor,
            kind,
        )


def _build_ignored_review_collection(col: Collection) -> frozenset[int]:
    """The replay collection, plus a card whose ignored review is its
    Learning start and a card whose ignored review is no start. Returns the
    ignored reviews, with one that is in no review log."""
    _build_replay_collection(col)
    _add_card_with_reviews(
        col,
        IGNORED_START_CARD,
        list(zip(IGNORED_START_REVIEWS, (RATED_REVIEW, LEARNING, RATED_REVIEW))),
    )
    _add_card_with_reviews(
        col,
        IGNORED_PLAIN_CARD,
        list(
            zip(
                IGNORED_PLAIN_REVIEWS,
                (LEARNING, RATED_REVIEW, RATED_REVIEW, RATED_RELEARNING),
            )
        ),
    )
    return frozenset({IGNORED_START_REVIEWS[1], IGNORED_PLAIN_REVIEWS[2], 12345})


def _rows_of(rows: list[Any], card_id: int) -> list[tuple[int, bool]]:
    """`(review id, is the start row)` of each of the card's rows."""
    return [(int(row[0]), bool(row[9])) for row in rows if int(row[1]) == card_id]


def test_every_replay_read_drops_the_ignored_reviews_before_the_start_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec sched.rwkv-replay-start-row: every read of the replay rows
    drops the ignored reviews before it finds a card's start row, as the
    backend fingerprint does: the query in one piece and in parts, the
    backend's rows, and a read after a review id. The fingerprint accepts
    the history the Python build makes of them."""
    col = Collection(str(tmp_path / "rwkv-replay-ignored.anki2"))
    try:
        ignored = _build_ignored_review_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        query = rwkv_scheduler._historical_rwkv_review_rows(
            reviewer, ignored_review_ids=ignored
        )
        backend = rwkv_scheduler._backend_historical_rwkv_review_rows(
            col, ignored_review_ids=ignored
        )
        assert backend is not None, "the backend did not answer"
        with monkeypatch.context() as patch:
            patch.setattr(
                rwkv_scheduler,
                "_backend_historical_rwkv_review_rows",
                lambda col, **_kwargs: None,
            )
            parts = rwkv_scheduler._historical_rwkv_review_rows(
                reviewer, between_parts=lambda: None, ignored_review_ids=ignored
            )
        assert len(query) > 50
        assert [tuple(row) for row in backend] == [tuple(row) for row in query]
        assert [tuple(row) for row in parts] == [tuple(row) for row in query]

        # the ignored Learning start is no start: the card starts at its
        # first rated row, as the fingerprint starts it
        first, _learning, last = IGNORED_START_REVIEWS
        assert _rows_of(query, IGNORED_START_CARD) == [(first, True), (last, False)]

        # every other card's history is the one it had when the ignored
        # reviews left the rows after the start rows were found
        unfiltered = rwkv_scheduler._historical_rwkv_review_rows(reviewer)
        dropped_after = [row for row in unfiltered if int(row[0]) not in ignored]
        assert [tuple(row) for row in query if int(row[1]) != IGNORED_START_CARD] == [
            tuple(row) for row in dropped_after if int(row[1]) != IGNORED_START_CARD
        ]
        assert _rows_of(query, IGNORED_PLAIN_CARD) == [
            (IGNORED_PLAIN_REVIEWS[0], True),
            (IGNORED_PLAIN_REVIEWS[1], False),
            (IGNORED_PLAIN_REVIEWS[3], False),
        ]
        # the old order read one review of the card, the fingerprint two
        assert _rows_of(dropped_after, IGNORED_START_CARD) == [(last, False)]

        # a read after a review id gives the whole read's rows after it
        review_ids = sorted(int(row[0]) for row in query)
        for cutoff in (first - 1, review_ids[len(review_ids) // 2], review_ids[-2]):
            for between_parts in (None, lambda: None):
                after = rwkv_scheduler._historical_rwkv_review_rows(
                    reviewer,
                    after_review_id=cutoff,
                    between_parts=between_parts,
                    ignored_review_ids=ignored,
                )
                assert [tuple(row) for row in after] == [
                    tuple(row) for row in query if int(row[0]) > cutoff
                ]

        # the Python build of the history, whose hash the fingerprint accepts
        # with the active ignored reviews the state cache stores
        monkeypatch.setattr(
            rwkv_scheduler,
            "_backend_historical_rwkv_review_inputs",
            lambda *_args, **_kwargs: None,
        )
        history = rwkv_scheduler._historical_rwkv_review_inputs(
            reviewer, ignored_review_ids=ignored
        )
        assert history.ignored_review_ids == tuple(sorted(ignored - {12345}))
        assert (
            rwkv_scheduler._historical_rwkv_review_inputs(
                reviewer, ignored_review_ids=ignored, between_steps=lambda: None
            )
            == history
        )
        fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
            reviewer,
            ignored_review_ids=history.ignored_review_ids,
            expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
                last_review_id=history.last_review_id,
                review_count=history.review_count,
                history_hash=history.history_hash,
            ),
        )
        assert fingerprint is not None and fingerprint.history_is_valid
        assert fingerprint.active_ignored_review_ids == history.ignored_review_ids
    finally:
        col.close()


def test_an_incremental_read_with_ignored_reviews_matches_the_whole_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incremental read with ignored reviews gives the whole read's result
    with the same ignored reviews: the rows after the cutoff, the counts, the
    identity and the active ignored reviews, also when the only new review
    is itself ignored."""
    monkeypatch.setattr(
        rwkv_scheduler,
        "_backend_historical_rwkv_review_inputs",
        lambda *_args, **_kwargs: None,
    )
    col = Collection(str(tmp_path / "rwkv-replay-ignored-incremental.anki2"))
    try:
        ignored = _build_ignored_review_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))

        def whole(ignored: frozenset[int]) -> RwkvHistoricalReviewInputs:
            return rwkv_scheduler._historical_rwkv_review_inputs(
                reviewer, ignored_review_ids=ignored
            )

        def incremental(
            before: RwkvHistoricalReviewInputs, ignored: frozenset[int]
        ) -> RwkvHistoricalReviewInputs:
            return rwkv_scheduler._historical_rwkv_review_inputs(
                reviewer,
                after_review_id=before.last_review_id,
                previous_review_id_by_card=dict(before.previous_review_id_by_card),
                previous_interval_days_by_card=dict(
                    before.previous_interval_days_by_card
                ),
                review_count_by_card=dict(before.review_count_by_card),
                previous_history_hash=before.history_hash,
                previous_replay_key=before.replay_key,
                ignored_review_ids=ignored,
            )

        def assert_same(
            incremental: RwkvHistoricalReviewInputs,
            whole: RwkvHistoricalReviewInputs,
            new_review_count: int,
        ) -> None:
            assert len(incremental.reviews) == new_review_count
            assert (
                incremental.reviews
                == whole.reviews[len(whole.reviews) - new_review_count :]
            )
            assert incremental.review_count == whole.review_count
            assert incremental.last_review_id == whole.last_review_id
            assert incremental.history_hash == whole.history_hash
            assert incremental.review_count_by_card == whole.review_count_by_card
            assert incremental.ignored_review_ids == whole.ignored_review_ids

        before = whole(ignored)
        # new reviews of the card whose Learning start is ignored, and of
        # another card
        day = 86_400 * 1000
        for n, card_id in enumerate((IGNORED_START_CARD, 1_700_000_000_000)):
            col.db.execute(
                "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, "
                "time, type) values (?, ?, -1, 3, 12, 10, 2500, 1000, 1)",
                before.last_review_id + (n + 1) * day,
                card_id,
            )
        after = whole(ignored)
        assert after.review_count == before.review_count + 2
        assert_same(incremental(before, ignored), after, 2)

        # nothing new: the saved state stands, with the same ignored reviews
        assert_same(incremental(after, ignored), after, 0)

        # the only new review is ignored as well
        only_ignored = after.last_review_id + day
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, "
            "time, type) values (?, ?, -1, 3, 12, 10, 2500, 1000, 1)",
            only_ignored,
            IGNORED_PLAIN_CARD,
        )
        more_ignored = ignored | {only_ignored}
        with_it_ignored = whole(more_ignored)
        assert with_it_ignored.history_hash == after.history_hash
        assert only_ignored in with_it_ignored.ignored_review_ids
        assert_same(incremental(after, more_ignored), with_it_ignored, 0)
    finally:
        col.close()


def test_a_single_card_read_is_the_cache_history_of_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec sched.rwkv-replay-start-row: Grade Now and a live answer read
    one card's rows with the resident state's active ignored reviews, through
    the query every history build uses, so they continue the resident state
    from the card's history as the cache holds it. An ignored Learning start
    is no start there either; an ignored review that is no start leaves the
    card's rows as they were before the ignored reviews left first."""
    monkeypatch.setattr(
        rwkv_scheduler,
        "_backend_historical_rwkv_review_inputs",
        lambda *_args, **_kwargs: None,
    )
    col = Collection(str(tmp_path / "rwkv-replay-ignored-card.anki2"))
    try:
        ignored = _build_ignored_review_collection(col)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        cache = rwkv_scheduler._historical_rwkv_review_inputs(
            reviewer, ignored_review_ids=ignored
        )
        # the resident state built from this history keeps its active
        # ignored reviews
        monkeypatch.setattr(
            rwkv_scheduler,
            "_resident_ignored_review_ids",
            lambda _reviewer: cache.ignored_review_ids,
        )
        whole = rwkv_scheduler._historical_rwkv_review_rows(
            reviewer, ignored_review_ids=ignored
        )
        for card_id in (IGNORED_START_CARD, IGNORED_PLAIN_CARD):
            rows = rwkv_scheduler._rwkv_card_history_rows(reviewer, [card_id])
            assert [tuple(row) for row in rows] == [
                tuple(row) for row in whole if int(row[1]) == card_id
            ]
            assert [int(row[0]) for row in rows] == [
                review_id
                for review_id, review in zip(
                    cache.review_ids, cache.reviews, strict=True
                )
                if review.identity.card_id == card_id
            ]
            history = rwkv_scheduler._rwkv_grade_now_card_histories(rows)[card_id]
            assert history.last_review_id == cache.previous_review_id_by_card[card_id]
            assert history.review_count == cache.review_count_by_card[card_id]

        # the ignored Learning start: the card starts at its first rated row
        first, _learning, last = IGNORED_START_REVIEWS
        assert _rows_of(
            rwkv_scheduler._rwkv_card_history_rows(reviewer, [IGNORED_START_CARD]),
            IGNORED_START_CARD,
        ) == [(first, True), (last, False)]

        # the ignored review that is no start: the rows of the old order
        before = [
            tuple(row)
            for row in rwkv_scheduler._historical_rwkv_review_rows(
                reviewer, card_ids=[IGNORED_PLAIN_CARD]
            )
            if int(row[0]) not in ignored
        ]
        assert [
            tuple(row)
            for row in rwkv_scheduler._rwkv_card_history_rows(
                reviewer, [IGNORED_PLAIN_CARD]
            )
        ] == before
    finally:
        col.close()

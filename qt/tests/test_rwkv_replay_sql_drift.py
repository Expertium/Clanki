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

from anki.collection import Collection
from anki.decks import DeckId
from aqt import rwkv_scheduler

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
    tmp_path: Path,
) -> None:
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
    col: Collection, card_id: int, rows: list[tuple[int, int, int]]
) -> None:
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
    tmp_path: Path,
) -> None:
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


def _build_replay_collection(col: Collection) -> None:
    learning = (3, 0, 2500)
    shapes = [
        [learning, RATED_REVIEW, RATED_RELEARNING, RATED_REVIEW],
        [RATED_REVIEW, RATED_REVIEW, FORGET, RATED_REVIEW, RATED_RELEARNING],
        [learning, learning, RATED_REVIEW, FORGET, learning, RATED_REVIEW],
        [RATED_REVIEW, RATED_RELEARNING],
    ]
    for n in range(25):
        _add_card_with_history(col, 1_700_000_000_000 + n * 7_919, shapes[n % 4])


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

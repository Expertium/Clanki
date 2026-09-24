# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.review-heatmap.

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from anki.collection import Config
from anki.decks import DeckId
from anki.utils import ids2str
from aqt import review_heatmap
from aqt.review_heatmap import (
    ADDON_NOTICE_SHOWN_KEY,
    ActivityReporter,
    HeatmapSettings,
    HeatmapView,
    ReviewHeatmap,
    compute_activity,
    disable_review_heatmap_addon,
    load_settings,
    render_report,
    settings_from_addon,
)

DAY = 86400


def test_compute_activity_streaks_average_and_active_share() -> None:
    today = 100 * DAY
    # 4 days in a row, a gap, then yesterday and today
    history = [
        (today - 9 * DAY, 10),
        (today - 8 * DAY, 20),
        (today - 7 * DAY, 30),
        (today - 6 * DAY, 40),
        (today - DAY, 5),
        (today, 15),
    ]
    forecast = [(today, -3), (today + DAY, -7)]
    report = compute_activity(history, forecast, today, offset=4)

    assert report.stats.streak_max == 4
    assert report.stats.streak_cur == 2
    assert report.stats.activity_daily_avg == 20  # 120 reviews / 6 active days
    assert report.stats.pct_days_active == 60  # 6 active days out of 10
    # history wins over the forecast for today; the forecast fills the future
    assert report.activity[today] == 15
    assert report.activity[today + DAY] == -7
    assert report.start == (today - 9 * DAY) * 1000
    assert report.stop == (today + DAY) * 1000
    assert report.today == today * 1000
    assert report.offset == 4


def test_current_streak_needs_activity_today_or_yesterday() -> None:
    today = 100 * DAY
    history = [(today - 3 * DAY, 1), (today - 2 * DAY, 1)]
    report = compute_activity(history, [], today, offset=4)
    assert report.stats.streak_max == 2
    assert report.stats.streak_cur == 0
    assert report.stop is None


def test_render_report_marks_view_and_falls_back_without_data() -> None:
    html = render_report(None, HeatmapView.overview, current_deck_only=True)
    assert "rh-view-overview" in html
    assert "No review activity" in html
    assert "cal-heatmap" not in html

    today = 100 * DAY
    report = compute_activity([(today, 12)], [(today + DAY, -4)], today, offset=4)
    html = render_report(report, HeatmapView.deckbrowser, current_deck_only=False)
    assert "rh-view-deckbrowser" in html
    assert "rh-theme-magenta" in html
    assert 'id="cal-heatmap"' in html
    assert '"whole": true' in html
    assert "12 cards" in html and "Current streak" in html
    assert "/_anki/js/vendor/anki-review-heatmap.js" in html


def test_the_settings_button_shows_the_deck_lists_gear() -> None:
    today = 100 * DAY
    report = compute_activity([(today, 12)], [], today, offset=4)
    html = render_report(report, HeatmapView.deckbrowser, current_deck_only=False)
    # the same gear the deck list draws, not the add-on's own three-bar mark
    assert "/_anki/imgs/gears.svg" in html
    assert "heatmap-options.svg" not in html
    # place and tooltip of the button are unchanged
    assert '<div class="hm-btn opts-btn" title="Settings' in html
    assert ".heatmap .heatmap-controls .hm-btn {" in html
    assert "width: 28px;" in html


# Pins spec/ui.md#ui.review-heatmap
def test_the_gear_is_drawn_larger_than_the_navigation_icons() -> None:
    today = 100 * DAY
    report = compute_activity([(today, 12)], [], today, offset=4)
    html = render_report(report, HeatmapView.deckbrowser, current_deck_only=False)
    navigation = html.split(".heatmap .heatmap-controls .hm-btn > img {")[1]
    navigation = navigation.split("}")[0]
    gear = html.split(".heatmap .heatmap-controls .opts-btn > img {")[1]
    gear = gear.split("}")[0]
    assert "height: 10px;" in navigation and "width: 10px;" in navigation
    assert "height: 14px;" in gear and "width: 14px;" in gear


def _heatmap(enabled: bool, stored: object = None) -> ReviewHeatmap:
    col = MagicMock()
    col.get_config_bool.side_effect = lambda key: (
        enabled if key == Config.Bool.REVIEW_HEATMAP_ENABLED else False
    )
    col.get_config.side_effect = lambda key, default=None: (
        stored if key == "reviewHeatmap" and stored is not None else default
    )
    return ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))


def test_nothing_is_drawn_while_the_preference_is_off() -> None:
    heatmap = _heatmap(enabled=False)
    content = SimpleNamespace(stats="<b>today</b>", table="<table></table>")
    heatmap.on_deck_browser_will_render_content(cast(Any, None), cast(Any, content))
    heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
    assert content.stats == "<b>today</b>"
    assert content.table == "<table></table>"


def test_a_new_collection_has_the_heatmap_on_and_keeps_a_stored_off(
    tmp_path: Any,
) -> None:
    from anki.collection import Collection

    path = str(tmp_path / "default.anki2")
    col = Collection(path)
    try:
        # a new collection draws the heatmap; the user finds no setting first
        assert col.get_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED)
        heatmap = ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))
        assert heatmap.enabled()
        # a user who turns it off keeps it off
        col.set_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED, False)
        assert not heatmap.enabled()
    finally:
        col.close(downgrade=False)

    col = Collection(path)
    try:
        assert not col.get_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED)
        heatmap = ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))
        assert not heatmap.enabled()
    finally:
        col.close(downgrade=False)


def test_render_uses_the_reporter_and_caches_per_input_fingerprint() -> None:
    heatmap = _heatmap(enabled=True)
    heatmap.mw.col.mod = 1
    with patch.object(review_heatmap, "ActivityReporter") as reporter:
        reporter.return_value.get_report.return_value = None
        reporter.return_value.input_fingerprint.return_value = ("inputs", 1)
        first = heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        second = heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        assert first == second and "rh-container" in first
        assert reporter.return_value.get_report.call_count == 1
        # a collection change that the report does not read keeps the cache
        heatmap.mw.col.mod = 2
        heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        assert reporter.return_value.get_report.call_count == 1
        # a change of what the report reads invalidates it
        reporter.return_value.input_fingerprint.return_value = ("inputs", 2)
        heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        assert reporter.return_value.get_report.call_count == 2


def test_input_fingerprint_follows_reviews_and_cards_only(tmp_path: Any) -> None:
    from anki.collection import Collection

    col = Collection(str(tmp_path / "heatmap.anki2"))
    try:
        note = col.new_note(col.models.current())
        note.fields[0] = "front"
        col.add_note(note, DeckId(1))
        reporter = ActivityReporter(col, HeatmapSettings())
        base = reporter.input_fingerprint(current_deck_only=False)

        # config writes and collapsing a deck change col.mod, not the report
        col.set_config("someUnrelatedKey", 42)
        deck = col.decks.get(DeckId(1))
        assert deck is not None
        deck["collapsed"] = not deck["collapsed"]
        col.decks.save(deck)
        assert reporter.input_fingerprint(current_deck_only=False) == base

        # moving a card to another deck changes it
        other = col.decks.id("Other")
        assert other is not None
        col.set_deck(note.card_ids(), other)
        moved = reporter.input_fingerprint(current_deck_only=False)
        assert moved != base

        # a review changes it
        col.decks.select(other)
        card = col.sched.getCard()
        assert card is not None
        col.sched.answerCard(card, 3)
        assert reporter.input_fingerprint(current_deck_only=False) != moved

        # a new subdeck changes the current-deck scope
        scoped = reporter.input_fingerprint(current_deck_only=True)
        col.decks.id("Other::Child")
        assert reporter.input_fingerprint(current_deck_only=True) != scoped

        # a DELETE without a WHERE clause empties a table without SQLite's
        # update hook; it still changes the fingerprint
        reviewed = reporter.input_fingerprint(current_deck_only=False)
        col.db.execute("delete from revlog")
        emptied = reporter.input_fingerprint(current_deck_only=False)
        assert emptied != reviewed
        col.db.execute("delete from cards")
        assert reporter.input_fingerprint(current_deck_only=False) != emptied
    finally:
        col.close(downgrade=False)


def test_render_keeps_one_cache_entry_per_place() -> None:
    heatmap = _heatmap(enabled=True)
    with patch.object(review_heatmap, "ActivityReporter") as reporter:
        reporter.return_value.get_report.return_value = None
        reporter.return_value.input_fingerprint.return_value = ("inputs", 1)
        for _ in range(2):
            heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
            heatmap.render(HeatmapView.overview, current_deck_only=True)
        # the deck list and the overview do not push each other out
        assert reporter.return_value.get_report.call_count == 2


def _reviews_per_day_in_one_query(
    col: Any, settings: HeatmapSettings, current_deck_only: bool
) -> list[tuple[int, int]]:
    """What _cards_done returned when it grouped the whole review log on
    every call: the reference its cache must match."""
    reporter = ActivityReporter(col, settings)
    where = []
    if settings.exclude_manual_reschedules:
        where.append("ease >= 1")
    dids = reporter._deck_ids(current_deck_only)
    if dids is not None:
        where.append(f"cid IN (SELECT id FROM cards WHERE did IN {ids2str(dids)})")
    elif settings.exclude_deleted_cards:
        where.append("cid IN (SELECT id FROM cards)")
    condition = f"WHERE {' AND '.join(where)}" if where else ""
    offset_secs = reporter._offset() * 3600
    return [
        (day, count)
        for day, count in col.db.all(
            f"""
SELECT CAST(STRFTIME('%s', id / 1000 - {offset_secs}, 'unixepoch',
                     'localtime', 'start of day') AS int) AS day, COUNT()
FROM revlog {condition}
GROUP BY day ORDER BY day"""
        )
    ]


def test_older_reviews_are_counted_once_and_newer_ones_every_time(
    tmp_path: Any,
) -> None:
    from anki.collection import Collection

    col = Collection(str(tmp_path / "heatmap.anki2"))
    try:
        other = col.decks.id("Other")
        assert other is not None
        card_ids = []
        for deck in (DeckId(1), DeckId(1), other):
            note = col.new_note(col.models.current())
            note.fields[0] = f"front {len(card_ids)}"
            col.add_note(note, deck)
            card_ids += note.card_ids()

        used_ids: set[int] = set()

        def add_review(card_id: int, days_ago: float, ease: int = 3) -> None:
            review_id = int(time.time() * 1000) - int(days_ago * DAY * 1000)
            # two reviews "12.5 days ago" in the same millisecond would share
            # an id, which the review log refuses (seen on the macOS runner)
            while review_id in used_ids:
                review_id -= 1
            used_ids.add(review_id)
            col.db.execute(
                "INSERT INTO revlog VALUES (?, ?, -1, ?, 1, 0, 2500, 1000, 1)",
                review_id,
                card_id,
                ease,
            )

        for days_ago in (40, 12.5, 12.25, 3, 1):
            add_review(card_ids[0], days_ago)
        add_review(card_ids[1], 30)
        add_review(card_ids[1], 20, ease=0)  # a manual reschedule
        add_review(card_ids[2], 12.5)
        col.decks.select(DeckId(1))
        cache: dict[Any, Any] = {}
        grouped: list[str] = []
        settings = HeatmapSettings(exclude_deleted_cards=True)

        def check() -> None:
            for current_deck_only in (False, True):
                reporter = ActivityReporter(col, settings, cache)
                original = reporter._review_days

                def review_days(dids: Any, condition: str) -> Any:
                    grouped.append(condition.split()[1])
                    return original(dids, condition)

                by_deck = reporter._review_days_by_deck

                def review_days_by_deck(cutoff: int) -> Any:
                    grouped.append("decks <")
                    return by_deck(cutoff)

                reporter._review_days = review_days  # type: ignore[method-assign]
                reporter._review_days_by_deck = review_days_by_deck  # type: ignore[method-assign]
                assert reporter._cards_done(
                    current_deck_only, None
                ) == _reviews_per_day_in_one_query(col, settings, current_deck_only)

        check()
        # one count per scope; a set of decks sums per-deck counts, made once
        assert grouped == ["<", ">=", "decks <", ">="]
        grouped.clear()

        # a new review is counted on its own; the older days are reused
        add_review(card_ids[2], 0)
        check()
        assert grouped == [">=", ">="]
        grouped.clear()

        # moving an older card changes the current deck's older days only;
        # the per-deck counts are made again
        col.set_deck([card_ids[2]], DeckId(1))
        check()
        assert grouped == [">=", "decks <", ">="]
        grouped.clear()

        # deleting a card with older reviews changes both
        col.remove_notes([col.get_card(card_ids[0]).nid])
        check()
        assert grouped == ["<", ">=", "decks <", ">="]
        grouped.clear()

        # a review imported from the past is older: both are counted again
        add_review(card_ids[1], 50)
        check()
        assert grouped == ["<", ">=", "decks <", ">="]
        grouped.clear()

        # another set of decks is a sum of the per-deck counts: no count
        other_scope = ActivityReporter(col, settings, cache)
        scope = [int(DeckId(1)), int(other)]
        older = other_scope._older_review_days(scope)
        assert older.cutoff == cache["by deck"].cutoff
        reference = ActivityReporter(col, settings)._review_days(
            scope, f"id < {older.cutoff}"
        )
        assert older.days == reference

        # the history start leaves out earlier days, as a filter on the day
        reporter = ActivityReporter(col, settings, cache)
        start = reporter._today() - 25 * DAY
        assert reporter._cards_done(False, start) == [
            row
            for row in _reviews_per_day_in_one_query(col, settings, False)
            if row[0] >= start
        ]
    finally:
        col.close(downgrade=False)


# a day per query, the default slice, and the whole history in one query
@pytest.mark.parametrize("chunk_days", [1, ActivityReporter._CHUNK_DAYS, 10**7])
def test_reviews_are_grouped_by_day_ranges_exactly_as_one_by_one(
    tmp_path: Any, monkeypatch: Any, chunk_days: int
) -> None:
    from anki.collection import Collection

    monkeypatch.setattr(ActivityReporter, "_CHUNK_DAYS", chunk_days)
    col = Collection(str(tmp_path / "heatmap.anki2"))
    try:
        other = col.decks.id("Other")
        assert other is not None
        card_ids = []
        for deck in (DeckId(1), other):
            note = col.new_note(col.models.current())
            note.fields[0] = f"front {deck}"
            col.add_note(note, deck)
            card_ids += note.card_ids()
        # reviews on every side of midnight and of the rollover hour, over
        # two years (daylight saving changes included, where the local time
        # zone has them), a manual reschedule, and a card deleted later
        base = 1_600_000_000_000
        review_ids = []
        for day in range(0, 730, 3):
            for minutes in (-61, -1, 0, 1, 59, 179, 180, 181, 239, 240, 241, 719):
                review_ids.append(base + day * DAY * 1000 + minutes * 60_000)
        for step, review_id in enumerate(sorted(review_ids)):
            col.db.execute(
                "INSERT INTO revlog VALUES (?, ?, -1, ?, 1, 0, 2500, 1000, 1)",
                review_id + step % 1000,
                card_ids[step % 2],
                0 if step % 17 == 0 else 3,
            )
        col.db.execute(
            "INSERT INTO revlog VALUES (?, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            base + 5,
            999,  # no such card
        )
        col.decks.select(other)
        for rollover in (0, 4, 23):
            col.set_config("rollover", rollover)
            for settings in (
                HeatmapSettings(),
                HeatmapSettings(
                    exclude_deleted_cards=True, exclude_manual_reschedules=False
                ),
            ):
                for current_deck_only in (False, True):
                    reporter = ActivityReporter(col, settings)
                    assert reporter._cards_done(
                        current_deck_only, None
                    ) == _reviews_per_day_in_one_query(col, settings, current_deck_only)
        # the day ranges were used, not the one-by-one grouping
        reporter = ActivityReporter(col, HeatmapSettings())
        assert reporter._review_days_by_day_ranges("id >= 0", "id >= 0") is not None
        # a review dated before 1970 leaves the grouping to one by one
        col.db.execute(
            "INSERT INTO revlog VALUES (-5000, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            card_ids[0],
        )
        assert reporter._review_days_by_day_ranges("id < 0", "id < 0") is None
        assert reporter._cards_done(False, None) == _reviews_per_day_in_one_query(
            col, HeatmapSettings(), False
        )
    finally:
        col.close(downgrade=False)


def test_fingerprint_sums_are_read_again_only_after_a_write(tmp_path: Any) -> None:
    from anki.collection import Collection

    col = Collection(str(tmp_path / "heatmap.anki2"))
    try:
        note = col.new_note(col.models.current())
        note.fields[0] = "front"
        col.add_note(note, DeckId(1))
        heatmap = ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))
        scans: list[str] = []

        def fingerprint() -> tuple[Any, ...]:
            reporter = ActivityReporter(
                col, HeatmapSettings(), heatmap._older_reviews, heatmap._contents
            )
            first = col.db.first

            def counting_first(sql: str, *args: Any) -> Any:
                # the scan of every card, not the write stamp read before it
                if "total(mod)" in sql:
                    scans.append(sql)
                return first(sql, *args)

            with patch.object(col.db, "first", counting_first):
                return reporter.input_fingerprint(current_deck_only=False)

        base = fingerprint()
        assert fingerprint() == base and len(scans) == 1
        # a write through SQL, which does not touch the modified time
        col.db.execute("UPDATE cards SET due = due + 1")
        changed = fingerprint()
        assert changed != base and len(scans) == 2
        # a write to neither the cards nor the review log does not read again:
        # selecting a deck writes the config
        col.set_config("someUnrelatedKey", 1)
        other = col.decks.id("Other")
        assert other is not None
        col.decks.select(other)
        assert fingerprint() == changed and len(scans) == 2
        # a review row does
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (1, 1, -1, 3, 1, 0, 2500, 1000, 1)"
        )
        reviewed = fingerprint()
        assert reviewed != changed and len(scans) == 3
        assert fingerprint() == reviewed and len(scans) == 3
        # reopening gives a new connection with its own write count
        col.close(downgrade=False)
        col.reopen()
        assert fingerprint() == reviewed and len(scans) == 4
    finally:
        col.close(downgrade=False)


def test_the_sums_read_before_a_draw_are_the_ones_the_draw_checks(
    tmp_path: Any,
) -> None:
    """A screen's background step reads the fingerprint's card scan, and the
    draw on the main thread then finds it kept: the same HTML, one scan. It
    reads nothing where the draw would read no fingerprint."""
    from anki.collection import Collection

    col = Collection(str(tmp_path / "heatmap.anki2"))
    try:
        note = col.new_note(col.models.current())
        note.fields[0] = "front"
        col.add_note(note, DeckId(1))
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (?, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            int(time.time() * 1000) - DAY * 1000,
            note.card_ids()[0],
        )
        scans: list[str] = []
        first = col.db.first

        def counting_first(sql: str, *args: Any) -> Any:
            if "total(mod)" in sql:
                scans.append(sql)
            return first(sql, *args)

        def session() -> ReviewHeatmap:
            return ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))

        view = HeatmapView.deckbrowser
        with patch.object(col.db, "first", counting_first):
            # no cached heatmap: the draw reads no fingerprint, nor does this
            heatmap = session()
            heatmap.read_fingerprint_sums(view, current_deck_only=False)
            assert scans == [] and heatmap._contents.last is None
            html = heatmap.render(view, current_deck_only=False)
            assert "rh-view-deckbrowser" in html
            twin = session()
            twin.render(view, current_deck_only=False)
            for write in ("UPDATE cards SET due = due + 1", None):
                if write:
                    col.db.execute(write)
                expected = twin.cached_html(view, current_deck_only=False)
                scans.clear()
                heatmap.read_fingerprint_sums(view, current_deck_only=False)
                # after the write the step scans once; with no write, not at all
                assert len(scans) == (1 if write else 0)
                assert heatmap.cached_html(view, current_deck_only=False) == expected
                # the draw scanned nothing
                assert len(scans) == (1 if write else 0)
            # the overview's place has no cached heatmap yet
            scans.clear()
            col.db.execute("UPDATE cards SET due = due + 1")
            heatmap.read_fingerprint_sums(HeatmapView.overview, current_deck_only=True)
            assert scans == []
            # nothing is read while the heatmap is off
            col.set_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED, False)
            heatmap.read_fingerprint_sums(view, current_deck_only=False)
            assert scans == []
    finally:
        col.close(downgrade=False)


def test_the_deck_list_and_overview_read_the_heatmap_sums_before_drawing(
    monkeypatch: Any,
) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    from aqt.deckbrowser import DeckBrowser
    from aqt.overview import Overview

    heatmap = MagicMock()
    mw = MagicMock()
    ops: list[Any] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append(op)

        def run_in_background(self) -> None:
            pass

    with (
        patch.object(review_heatmap, "instance", return_value=heatmap),
        patch("aqt.deckbrowser.QueryOp", FakeQueryOp),
        patch("aqt.overview.QueryOp", FakeQueryOp),
        patch("aqt.rwkv_scheduler.rwkv_state_cache_loading", return_value=False),
        patch("aqt.rwkv_scheduler.rwkv_review_scores_pending", return_value=False),
        patch("aqt.rwkv_scheduler.prepare_current_deck_review_queue_scores"),
        patch("aqt.rwkv_scheduler.clear_deck_browser_rwkv_count_scores"),
        patch("aqt.rwkv_scheduler.deck_browser_rwkv_count_scope_ids", return_value=()),
    ):
        DeckBrowser(mw).refresh()
        ops[-1](mw.col)
        heatmap.read_fingerprint_sums.assert_called_once_with(
            HeatmapView.deckbrowser, False
        )
        Overview(mw).refresh()
        ops[-1](mw.col)
        heatmap.read_fingerprint_sums.assert_called_with(HeatmapView.overview, True)
        assert heatmap.read_fingerprint_sums.call_count == 2


def test_the_background_step_fills_the_cache_and_reports_an_error() -> None:
    heatmap, _drawn = _fill_in_heatmap()
    ops: list[tuple[Any, Any]] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append((op, success))

        def run_in_background(self) -> None:
            pass

    with (
        patch("aqt.operations.QueryOp", FakeQueryOp),
        patch.object(review_heatmap, "ActivityReporter") as reporter,
    ):
        reporter.return_value.get_report.return_value = None
        reporter.return_value.input_fingerprint.return_value = ("inputs", 1)
        heatmap._schedule_fill_in(HeatmapView.overview, current_deck_only=True)
        assert ops[0][0](heatmap.mw.col) is True
        assert reporter.return_value.get_report.call_count == 1
        # the draw on the main thread finds the report in the cache
        html = heatmap.render(HeatmapView.overview, current_deck_only=True)
        assert "rh-view-overview" in html
        assert reporter.return_value.get_report.call_count == 1
        # a report that raises is caught, and reported as not computed
        reporter.return_value.input_fingerprint.side_effect = RuntimeError("db")
        heatmap._schedule_fill_in(HeatmapView.deckbrowser, current_deck_only=False)
        assert ops[1][0](heatmap.mw.col) is False


def test_the_deck_list_and_overview_draw_without_waiting_for_the_heatmap(
    monkeypatch: Any,
) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    from aqt.deckbrowser import DeckBrowser
    from aqt.overview import Overview

    heatmap = MagicMock()
    mw = MagicMock()
    mw.col.sched._is_finished.return_value = False
    ops: list[Any] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append(op)

        def run_in_background(self) -> None:
            pass

    with (
        patch.object(review_heatmap, "instance", return_value=heatmap),
        patch("aqt.deckbrowser.QueryOp", FakeQueryOp),
        patch("aqt.overview.QueryOp", FakeQueryOp),
        patch("aqt.rwkv_scheduler.rwkv_state_cache_loading", return_value=False),
        patch("aqt.rwkv_scheduler.rwkv_review_scores_pending", return_value=False),
        patch("aqt.rwkv_scheduler.prepare_current_deck_review_queue_scores"),
        patch("aqt.rwkv_scheduler.clear_deck_browser_rwkv_count_scores"),
        patch("aqt.rwkv_scheduler.deck_browser_rwkv_count_scope_ids", return_value=()),
    ):
        DeckBrowser(mw).refresh()
        ops[-1](mw.col)
        Overview(mw).refresh()
        ops[-1](mw.col)
    # neither screen's background step computes a heatmap any more: they draw
    # their counts, and the heatmap fills in behind them
    heatmap.prepare.assert_not_called()


def _fill_in_heatmap() -> tuple[ReviewHeatmap, list[Any]]:
    """A heatmap whose mw records the screen it draws, with a cold cache."""
    heatmap = _heatmap(enabled=True)
    drawn: list[Any] = []
    deck_browser = MagicMock()
    deck_browser._renderPage.side_effect = lambda **kw: drawn.append(
        ("deckBrowser", kw)
    )
    overview = MagicMock()
    overview._renderPage.side_effect = lambda: drawn.append(("overview", {}))
    heatmap.mw = cast(
        Any,
        SimpleNamespace(
            col=heatmap.mw.col,
            pm=None,
            state="overview",
            deckBrowser=deck_browser,
            overview=overview,
        ),
    )
    return heatmap, drawn


def test_a_screen_without_its_heatmap_draws_it_in_the_background_and_again() -> None:
    heatmap, drawn = _fill_in_heatmap()
    ops: list[tuple[Any, Any]] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append((op, success))

        def run_in_background(self) -> None:
            pass

    with patch("aqt.operations.QueryOp", FakeQueryOp):
        content = SimpleNamespace(stats="", table="<table></table>")
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        # the screen draws now, with no heatmap and no wait
        assert content.table == "<table></table>"
        assert len(ops) == 1
        # a second draw before the first finishes asks for nothing more
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        assert len(ops) == 1

        op, success = ops[0]
        with patch.object(heatmap, "render", return_value="<div>heatmap</div>"):
            assert op(heatmap.mw.col) is True
        assert drawn == []
        success(True)
        assert drawn == [("overview", {})]


def test_a_heatmap_that_cannot_be_computed_is_not_drawn_again() -> None:
    heatmap, drawn = _fill_in_heatmap()
    ops: list[tuple[Any, Any]] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append((op, success))

        def run_in_background(self) -> None:
            pass

    with patch("aqt.operations.QueryOp", FakeQueryOp):
        content = SimpleNamespace(stats="", table="")
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        op, success = ops[0]
        with patch.object(heatmap, "render", side_effect=RuntimeError("no")):
            assert op(heatmap.mw.col) is False
        success(False)
    # the report failed, so the screen is left as it is rather than drawn
    # again, which would ask for the same report for ever
    assert drawn == []


def test_a_deck_opened_while_the_report_ran_gets_its_own_heatmap() -> None:
    heatmap, drawn = _fill_in_heatmap()
    ops: list[tuple[Any, Any]] = []

    class FakeQueryOp:
        def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
            ops.append((op, success))

        def run_in_background(self) -> None:
            pass

    with patch("aqt.operations.QueryOp", FakeQueryOp):
        content = SimpleNamespace(stats="", table="")
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        # the user opens a second deck while the first report is computed
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        assert len(ops) == 1
        ops[0][1](True)
        # the second draw asks for the report of the deck now open
        assert drawn == [("overview", {})]
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
        assert len(ops) == 2


def test_a_ready_heatmap_is_drawn_at_once_with_no_background_step() -> None:
    heatmap, _drawn = _fill_in_heatmap()
    ops: list[Any] = []

    class FakeQueryOp:
        def __init__(self, **kwargs: Any) -> None:
            ops.append(kwargs)

        def run_in_background(self) -> None:
            pass

    with (
        patch("aqt.operations.QueryOp", FakeQueryOp),
        patch.object(heatmap, "cached_html", return_value="<div>heatmap</div>"),
    ):
        content = SimpleNamespace(stats="", table="<table></table>")
        heatmap.on_overview_will_render_content(cast(Any, None), cast(Any, content))
    assert content.table == "<table></table><div>heatmap</div>"
    assert ops == []


def test_a_cold_cache_is_reported_without_reading_the_collection() -> None:
    heatmap = _heatmap(enabled=True)
    with patch.object(
        ActivityReporter, "input_fingerprint", side_effect=AssertionError("read")
    ):
        assert heatmap.cached_html(HeatmapView.overview, current_deck_only=True) is None


def test_clicking_a_day_opens_the_browser_with_the_search() -> None:
    heatmap = _heatmap(enabled=True)
    with patch("aqt.dialogs.open") as open_dialog:
        handled = heatmap.on_webview_did_receive_js_message(
            (False, None), "revhm_browse:prop:rated=-3", None
        )
        assert handled == (True, None)
        open_dialog.assert_called_once_with(
            "Browser", heatmap.mw, search=("prop:rated=-3",)
        )
        # other messages pass through untouched
        assert heatmap.on_webview_did_receive_js_message(
            (False, None), "study", None
        ) == (False, None)


def test_settings_keep_defaults_for_missing_or_invalid_values() -> None:
    assert HeatmapSettings.from_config(None) == HeatmapSettings()
    settings = HeatmapSettings.from_config(
        {
            "colors": "ice",
            "mode": "months",
            "history_limit_days": 90,
            "forecast_limit_days": -3,
            "exclude_deleted_cards": "yes",
            "excluded_decks": [5, "x", True, 7],
            "show_on_stats": False,
        }
    )
    assert settings.colors == "ice" and settings.mode == "months"
    assert settings.history_limit_days == 90
    assert settings.forecast_limit_days == 0  # negative is invalid
    assert settings.exclude_deleted_cards is False  # not a bool
    assert settings.excluded_decks == (5, 7)
    assert settings.show_on_stats is False
    assert HeatmapSettings.from_config({"colors": "rainbow"}).colors == "magenta"
    # a round trip through the stored form changes nothing
    assert HeatmapSettings.from_config(settings.to_config()) == settings


def test_the_addons_settings_are_carried_over_except_its_default_colour() -> None:
    synced = {
        "colors": "lime",
        "mode": "months",
        "limhist": 30,
        "limdate": 1_600_000_000,
        "limfcst": 10,
        "limcdel": True,
        "limresched": False,
        "limdecks": [3],
    }
    profile = {
        "display": {"deckbrowser": True, "overview": False, "stats": True},
        "statsvis": False,
    }
    settings = settings_from_addon(synced, profile)
    assert settings == HeatmapSettings(
        colors="magenta",
        mode="months",
        history_limit_days=30,
        ignore_before=1_600_000_000,
        forecast_limit_days=10,
        exclude_deleted_cards=True,
        exclude_manual_reschedules=False,
        excluded_decks=(3,),
        show_on_deck_list=True,
        show_on_overview=False,
        show_on_stats=True,
        streak_stats_always=False,
    )
    assert settings_from_addon({"colors": "flame"}, None).colors == "flame"


def test_clanki_settings_win_over_the_addons_once_saved() -> None:
    col = MagicMock()
    stored = {"reviewHeatmap": {"colors": "olive"}, "heatmap": {"colors": "flame"}}
    col.get_config.side_effect = lambda key, default=None: stored.get(key, default)
    assert load_settings(col).colors == "olive"
    del stored["reviewHeatmap"]
    assert load_settings(col).colors == "flame"
    del stored["heatmap"]
    assert load_settings(col) == HeatmapSettings()


def test_render_follows_colour_mode_and_visibility_settings() -> None:
    today = 100 * DAY
    report = compute_activity([(today, 12)], [], today, offset=4)
    settings = HeatmapSettings(colors="flame", mode="months", show_on_overview=False)
    html = render_report(report, HeatmapView.deckbrowser, False, settings)
    assert "rh-theme-flame" in html and "rh-mode-months" in html
    assert '"domain": "month"' in html and '"range": 9' in html
    # hidden on the overview: no calendar, the streak figures stay
    html = render_report(report, HeatmapView.overview, True, settings)
    assert 'id="cal-heatmap"' not in html and "Current streak" in html
    assert "rh-disable-heatmap" in html


def test_nothing_is_computed_where_heatmap_and_figures_are_hidden() -> None:
    heatmap = _heatmap(
        enabled=True,
        stored={"show_on_overview": False, "streak_stats_always": False},
    )
    with patch.object(review_heatmap, "ActivityReporter") as reporter:
        assert heatmap.render(HeatmapView.overview, current_deck_only=True) == ""
        reporter.assert_not_called()


def test_stats_screen_uses_its_period_and_scope() -> None:
    heatmap = _heatmap(enabled=True)
    heatmap.mw.col.mod = 1
    heatmap.mw.col.decks.get_current_id.return_value = 7
    with patch.object(review_heatmap, "ActivityReporter") as reporter:
        reporter.return_value.get_report.return_value = None
        heatmap.render_for_stats(period=1, whole_collection=True)
        reporter.return_value.get_report.assert_called_with(False, 365, 365)
        heatmap.render_for_stats(period=2, whole_collection=False)
        reporter.return_value.get_report.assert_called_with(True, None, None)


def test_shift_clicks_cycle_the_mode_and_the_colours() -> None:
    heatmap = _heatmap(enabled=True, stored={"colors": "flame", "mode": "months"})
    screen = MagicMock()
    heatmap.on_webview_did_receive_js_message(
        (False, None), "revhm_themeswitch", screen
    )
    saved = heatmap.mw.col.set_config.call_args.args
    assert saved[0] == "reviewHeatmap" and saved[1]["colors"] == "lime"
    heatmap.on_webview_did_receive_js_message((False, None), "revhm_modeswitch", screen)
    assert heatmap.mw.col.set_config.call_args.args[1]["mode"] == "year"
    assert screen.refresh.call_count == 2


def test_shift_click_on_the_gear_swaps_the_colour_class_in_place() -> None:
    from aqt.deckbrowser import DeckBrowser

    heatmap = _heatmap(enabled=True, stored={"colors": "flame"})
    screen = MagicMock(spec=DeckBrowser)
    screen.web = MagicMock()
    screen._rendered_stats = '<div class="rh-container rh-theme-flame rh-mode-year">'
    heatmap.on_webview_did_receive_js_message(
        (False, None), "revhm_themeswitch", screen
    )
    assert heatmap.mw.col.set_config.call_args.args[1]["colors"] == "lime"
    # the colours are CSS only: the class changes in the open page
    js = screen.web.eval.call_args.args[0]
    assert "rh-theme-flame" in js and 'classList.add("rh-theme-lime")' in js
    screen._renderPage.assert_not_called()
    assert screen._rendered_stats == (
        '<div class="rh-container rh-theme-lime rh-mode-year">'
    )


def test_the_settings_link_opens_the_heatmap_tab_of_preferences() -> None:
    heatmap = _heatmap(enabled=True)
    with patch("aqt.dialogs.open") as open_dialog:
        heatmap.on_webview_did_receive_js_message((False, None), "revhm_opts", None)
        open_dialog.assert_called_once_with("Preferences", heatmap.mw)
        open_dialog.return_value.show_review_heatmap_tab.assert_called_once()


def test_an_enabled_review_heatmap_addon_is_disabled() -> None:
    enabled = {"1771074083": True, "723520343": False, "other_addon": True}
    toggled: list[tuple[str, bool]] = []
    manager = SimpleNamespace(
        allAddons=lambda: list(enabled),
        isEnabled=lambda folder: enabled[folder],
        toggleEnabled=lambda folder, enable: toggled.append((folder, enable)),
    )
    assert disable_review_heatmap_addon(manager) == ["1771074083"]
    assert toggled == [("1771074083", False)]


def test_the_addon_notice_is_shown_only_once() -> None:
    from aqt.main import AnkiQt

    shown: list[str] = []
    mw = cast(
        Any,
        SimpleNamespace(
            _review_heatmap_addon_notice_pending=True,
            pm=SimpleNamespace(meta={}, save=MagicMock()),
        ),
    )
    with patch("aqt.main.showInfo", side_effect=lambda text, **_: shown.append(text)):
        AnkiQt._show_review_heatmap_addon_notice(mw)
        AnkiQt._show_review_heatmap_addon_notice(mw)
    assert len(shown) == 1
    assert mw.pm.meta[ADDON_NOTICE_SHOWN_KEY] is True
    mw.pm.save.assert_called_once()


# Pins spec/ui.md#ui.review-heatmap
def test_enabling_the_review_heatmap_addon_is_refused_with_a_message() -> None:
    from aqt.addons import AddonManager

    written: list[bool] = []
    for folder, expect_enabled in (("1771074083", False), ("other_addon", True)):
        addon = SimpleNamespace(enabled=False, human_name=lambda: folder)
        manager = MagicMock()
        manager.addon_meta.return_value = addon
        manager._disableConflicting.return_value = []
        manager.write_addon_meta.side_effect = lambda meta: written.append(meta.enabled)
        with (
            patch("aqt.addons.showInfo") as show_info,
            patch("aqt.addons.tr") as tr,
        ):
            AddonManager.toggleEnabled(manager, folder, enable=True)
        assert addon.enabled is expect_enabled
        if expect_enabled:
            show_info.assert_not_called()
        else:
            show_info.assert_called_once_with(
                tr.preferences_heatmap_addon_blocked.return_value, textFormat="plain"
            )
    assert written == [False, True]


# Pins spec/ui.md#ui.review-heatmap
def test_installing_the_review_heatmap_addon_leaves_it_disabled() -> None:
    import io
    import zipfile

    from aqt.addons import AddonManager

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w"):
        pass
    manager = MagicMock()
    manager.readManifestFile.return_value = {
        "package": "1771074083",
        "name": "Review Heatmap",
    }
    manager._manifest_schema = {"properties": {}}
    manager.addonMeta.return_value = {"name": "Review Heatmap"}
    manager._disableConflicting.return_value = []
    with (
        patch("aqt.addons.showInfo") as show_info,
        patch("aqt.addons.tr"),
        patch("aqt.addons.gui_hooks"),
    ):
        AddonManager.install(manager, archive, force_enable=True)
    package, meta = manager.writeAddonMeta.call_args.args
    assert package == "1771074083" and meta["disabled"] is True
    show_info.assert_called_once()


def test_forecast_never_reaches_past_five_years() -> None:
    """The due forecast stops 5 years (1,826 days) ahead, whatever the
    setting or the period (spec ui.review-heatmap)."""
    today = 100 * DAY

    def forecast_stop(settings_days: int, forecast_days: int | None) -> int:
        reporter = review_heatmap.ActivityReporter(
            cast(Any, MagicMock()),
            HeatmapSettings(forecast_limit_days=settings_days),
        )
        stops: list[int] = []
        with (
            patch.object(reporter, "_today", return_value=today),
            patch.object(reporter, "_cards_done", return_value=[(today, 1)]),
            patch.object(reporter, "_offset", return_value=4),
            patch.object(
                reporter,
                "_cards_due",
                side_effect=lambda start, stop, current_deck_only: (
                    stops.append(stop) or []
                ),
            ),
        ):
            reporter.get_report(False, None, forecast_days)
        return (stops[0] - today) // DAY

    assert review_heatmap.MAX_FORECAST_DAYS == 1826
    assert forecast_stop(0, None) == 1826  # "no limit" = 5 years
    assert forecast_stop(10, None) == 10
    assert forecast_stop(5000, None) == 1826
    assert forecast_stop(0, 3650) == 1826  # the stats screen's period
    assert forecast_stop(0, 365) == 365


def test_the_deck_list_no_longer_warms_the_overview_heatmap_after_2_s() -> None:
    """The warm-up used to start the overview's heatmap 2 s after the deck
    list was drawn, so that the first deck opened found it ready. It landed
    on the one collection worker at the moment a user clicks a deck, and the
    click then waited about 780 ms for it. The screens no longer wait for the
    heatmap at all (spec ui.review-heatmap-fills-in), so the warm-up is gone."""
    heatmap = _heatmap(enabled=True)
    assert not hasattr(heatmap, "on_deck_browser_did_render")
    shots: list[Any] = []
    heatmap.mw = cast(
        Any,
        SimpleNamespace(
            col=heatmap.mw.col,
            pm=None,
            progress=SimpleNamespace(
                single_shot=lambda ms, func, *args: shots.append((ms, func))
            ),
        ),
    )
    content = SimpleNamespace(stats="<b>today</b>", table="")
    with patch("aqt.operations.QueryOp", MagicMock()):
        heatmap.on_deck_browser_will_render_content(cast(Any, None), cast(Any, content))
    assert shots == []


# Pins spec/ui.md#ui.review-heatmap-kept-counts


def _collection_with_reviews(path: str) -> Any:
    from anki.collection import Collection

    col = Collection(path)
    other = col.decks.id("Other")
    assert other is not None
    card_ids = []
    for deck in (DeckId(1), other):
        note = col.new_note(col.models.current())
        note.fields[0] = f"front {len(card_ids)}"
        col.add_note(note, deck)
        card_ids += note.card_ids()
    now = int(time.time() * 1000)
    for card_id, days_ago in ((card_ids[0], 30), (card_ids[0], 3), (card_ids[1], 9)):
        col.db.execute(
            "INSERT INTO revlog VALUES (?, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            now - days_ago * DAY * 1000,
            card_id,
        )
    col.decks.select(DeckId(1))
    return col


def _session(col: Any) -> ReviewHeatmap:
    return ReviewHeatmap(cast(Any, SimpleNamespace(col=col, pm=None)))


def test_the_next_session_draws_from_the_kept_counts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    col = _collection_with_reviews(str(tmp_path / "collection.anki2"))
    try:
        first = _session(col)
        deck_list = first.render(HeatmapView.deckbrowser, False)
        overview = first.render(HeatmapView.overview, True)
        assert (tmp_path / "collection.heatmap-cache.json").exists()

        # a new session: the same heatmaps, with no pass over the older reviews
        original = ActivityReporter._review_days
        original_by_deck = ActivityReporter._review_days_by_deck
        passes: list[str] = []

        def review_days(self: Any, dids: Any, condition: str) -> Any:
            passes.append(condition.split()[1])
            return original(self, dids, condition)

        def review_days_by_deck(self: Any, cutoff: int) -> Any:
            passes.append("decks <")
            return original_by_deck(self, cutoff)

        monkeypatch.setattr(ActivityReporter, "_review_days", review_days)
        monkeypatch.setattr(
            ActivityReporter, "_review_days_by_deck", review_days_by_deck
        )
        second = _session(col)
        assert second.render(HeatmapView.overview, True) == overview
        assert second.render(HeatmapView.deckbrowser, False) == deck_list
        assert "<" not in passes and "decks <" not in passes

        # a review added since is counted with the newer ones; still no pass
        col.db.execute(
            "INSERT INTO revlog VALUES (?, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            int(time.time() * 1000),
            col.db.scalar("SELECT min(id) FROM cards"),
        )
        passes.clear()
        third = _session(col)
        fresh = _session(col)
        fresh._kept_for = col.path  # a session that has no kept counts
        assert third.render(HeatmapView.overview, True) == fresh.render(
            HeatmapView.overview, True
        )
        assert passes.count("decks <") == 1  # the fresh session's own pass
    finally:
        col.close(downgrade=False)


def test_kept_counts_that_no_longer_fit_are_made_again(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    col = _collection_with_reviews(str(tmp_path / "collection.anki2"))
    try:
        _session(col).render(HeatmapView.overview, True)
        path = str(tmp_path / "collection.anki2")
        kept = review_heatmap.read_kept_counts(path)
        assert set(kept) == {"by deck"}

        # a review imported from the past changes the older reviews
        col.db.execute(
            "INSERT INTO revlog VALUES (?, ?, -1, 3, 1, 0, 2500, 1000, 1)",
            int(time.time() * 1000) - 50 * DAY * 1000,
            col.db.scalar("SELECT min(id) FROM cards"),
        )
        session = _session(col)
        before = kept["by deck"]
        html = session.render(HeatmapView.overview, True)
        after = session._older_reviews["by deck"]
        assert after.cutoff > before.cutoff
        # the new counts are kept in place of the old ones
        assert review_heatmap.read_kept_counts(path)["by deck"].cutoff == after.cutoff
        fresh = _session(col)
        fresh._kept_for = col.path
        assert html == fresh.render(HeatmapView.overview, True)

        # counts kept more than a week ago are not used
        monkeypatch.setattr(
            review_heatmap.time,
            "time",
            lambda: after.cutoff / 1000 + 8 * DAY,
        )
        assert review_heatmap.read_kept_counts(path) == {}
    finally:
        col.close(downgrade=False)


def test_a_damaged_or_foreign_kept_file_is_ignored(tmp_path: Any) -> None:
    path = str(tmp_path / "collection.anki2")
    kept = tmp_path / "collection.heatmap-cache.json"
    for text in ("{not json", '{"version": 99, "counts": {}}', '{"version": 1}', "[]"):
        kept.write_text(text, encoding="utf-8")
        assert review_heatmap.read_kept_counts(path) == {}


def test_kept_counts_read_back_as_they_were(tmp_path: Any) -> None:
    from aqt.review_heatmap import _OlderReviews, _OlderReviewsByDeck

    key = ("C:/x/collection.anki2", 4, 10800, False, True, (12, 1700000000000))
    counts = {
        "by deck": _OlderReviewsByDeck(
            key, int(time.time() * 1000), {1: (3, 4.5e12)}, {1: {19000: 2, 19001: 1}}
        ),
        None: _OlderReviews(
            (*key, (3, 4.5e12)), int(time.time() * 1000), {19000: 2, 19001: 1}
        ),
        (1, 2): _OlderReviews(key, 1, {}),  # a set of decks is not kept
    }
    path = str(tmp_path / "collection.anki2")
    review_heatmap.write_kept_counts(path, counts)
    assert review_heatmap.read_kept_counts(path) == {
        "by deck": counts["by deck"],
        None: counts[None],
    }

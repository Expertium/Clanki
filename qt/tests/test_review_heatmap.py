# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.review-heatmap.

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

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

        def add_review(card_id: int, days_ago: float, ease: int = 3) -> None:
            review_id = int(time.time() * 1000) - int(days_ago * DAY * 1000)
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

                reporter._review_days = review_days  # type: ignore[method-assign]
                assert reporter._cards_done(
                    current_deck_only, None
                ) == _reviews_per_day_in_one_query(col, settings, current_deck_only)

        check()
        assert grouped == ["<", ">=", "<", ">="]  # one count per scope
        grouped.clear()

        # a new review is counted on its own; the older days are reused
        add_review(card_ids[2], 0)
        check()
        assert grouped == [">=", ">="]
        grouped.clear()

        # moving an older card changes the current deck's older days only
        col.set_deck([card_ids[2]], DeckId(1))
        check()
        assert grouped == [">=", "<", ">="]
        grouped.clear()

        # deleting a card with older reviews changes both
        col.remove_notes([col.get_card(card_ids[0]).nid])
        check()
        assert grouped == ["<", ">=", "<", ">="]
        grouped.clear()

        # a review imported from the past is older: both are counted again
        add_review(card_ids[1], 50)
        check()
        assert grouped == ["<", ">=", "<", ">="]

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

# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.review-heatmap.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from anki.collection import Config
from aqt import review_heatmap
from aqt.review_heatmap import (
    ADDON_NOTICE_SHOWN_KEY,
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


def test_render_uses_the_reporter_and_caches_per_collection_mod() -> None:
    heatmap = _heatmap(enabled=True)
    heatmap.mw.col.mod = 1
    heatmap.mw.col.decks.get_current_id.return_value = 7
    with patch.object(review_heatmap, "ActivityReporter") as reporter:
        reporter.return_value.get_report.return_value = None
        first = heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        second = heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        assert first == second and "rh-container" in first
        assert reporter.return_value.get_report.call_count == 1
        # a collection change invalidates the cache
        heatmap.mw.col.mod = 2
        heatmap.render(HeatmapView.deckbrowser, current_deck_only=False)
        assert reporter.return_value.get_report.call_count == 2


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

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
    HeatmapView,
    ReviewHeatmap,
    compute_activity,
    render_report,
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


def _heatmap(enabled: bool) -> ReviewHeatmap:
    col = MagicMock()
    col.get_config_bool.side_effect = lambda key: (
        enabled if key == Config.Bool.REVIEW_HEATMAP_ENABLED else False
    )
    return ReviewHeatmap(cast(Any, SimpleNamespace(col=col)))


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

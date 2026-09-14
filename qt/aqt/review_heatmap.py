# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The review heatmap on the deck list and the deck overview.

Ported into Clanki from Glutanimate's "Review Heatmap" add-on
(https://github.com/glutanimate/review-heatmap, GNU AGPLv3), with its
default look (lime colours, yearly overview) and without the add-on's
options dialog: the one setting is the "Show the review heatmap"
checkbox in Preferences > Review (spec ui.review-heatmap).

The calendar itself is drawn by the add-on's JS bundle (cal-heatmap on
d3), vendored under qt/aqt/data/web/js/vendor/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Sequence

from anki.collection import Config
from anki.utils import ids2str, is_mac, is_win
from aqt import gui_hooks

if TYPE_CHECKING:
    from anki.collection import Collection
    from aqt.deckbrowser import DeckBrowser, DeckBrowserContent
    from aqt.main import AnkiQt
    from aqt.overview import Overview, OverviewContent

MAX_FORECAST_DAYS = 73000
DEFAULT_ROLLOVER = 4

WEB_BASE = "/_anki"
PLATFORM = "mac" if is_mac else ("win" if is_win else "lin")


class HeatmapView(Enum):
    deckbrowser = "deckbrowser"
    overview = "overview"


# Activity report
######################################################################


@dataclass(frozen=True)
class HeatmapStats:
    streak_max: int
    streak_cur: int
    pct_days_active: int
    activity_daily_avg: int


@dataclass(frozen=True)
class ActivityReport:
    activity: dict[int, int]
    """day start (seconds) -> reviews done (positive) or cards due (negative)"""
    start: int | None
    """first day with history, in ms (cal-heatmap wants ms)"""
    stop: int | None
    """last forecast day, in ms"""
    today: int
    """today's day start, in ms"""
    offset: int
    """the 'next day starts at' hour"""
    stats: HeatmapStats


def compute_activity(
    history: Sequence[Sequence[int]],
    forecast: Sequence[Sequence[int]],
    today: int,
    offset: int,
) -> ActivityReport:
    """Streaks, averages and the merged day->count map.

    `history` and `forecast` are (day start in seconds, count) rows sorted
    by day; forecast counts are negative so the calendar can colour the
    future differently. `history` must not be empty.
    """
    first_day = history[0][0]
    last_day = forecast[-1][0] if forecast else 0

    streak_max = 0
    streak_cur = 0
    streak_last = 0
    current = 0
    total = 0
    idx = 0

    for idx, item in enumerate(history):
        current += 1
        timestamp, count = item
        next_timestamp = history[idx + 1][0] if idx + 1 < len(history) else None
        if next_timestamp is None:
            streak_last = current
        if timestamp + 86400 != next_timestamp:  # a gap of more than a day
            streak_max = max(streak_max, current)
            current = 0
        total += count

    days_learned = idx + 1

    # the current streak counts only while the last active day is today or
    # yesterday
    if history[-1][0] in (today, today - 86400):
        streak_cur = streak_last

    avg_cur = int(round(total / max(days_learned, 1)))

    # share of days with activity since the first recorded review
    days_total = (today - first_day) / 86400 + 1
    if days_total <= 1:
        pdays = 100
    else:
        pdays = int(round((days_learned / days_total) * 100))

    activity: dict[int, int] = {int(day): int(n) for day, n in forecast}
    activity.update({int(day): int(n) for day, n in history})  # history wins for today

    return ActivityReport(
        activity=activity,
        start=first_day * 1000 if first_day else None,
        stop=last_day * 1000 if last_day else None,
        today=today * 1000,
        offset=offset,
        stats=HeatmapStats(
            streak_max=streak_max,
            streak_cur=streak_cur,
            pct_days_active=pdays,
            activity_daily_avg=avg_cur,
        ),
    )


class ActivityReporter:
    """Reads review history and the due forecast from the collection."""

    def __init__(self, col: Collection) -> None:
        self._col = col

    def get_report(self, current_deck_only: bool) -> ActivityReport | None:
        today = self._today()
        history = self._cards_done(current_deck_only)
        if not history:
            return None
        forecast = self._cards_due(
            start=today,
            stop=today + 86400 * MAX_FORECAST_DAYS,
            current_deck_only=current_deck_only,
        )
        return compute_activity(history, forecast, today, self._offset())

    def _offset(self) -> int:
        """The 'next day starts at' hour."""
        return int(self._col.get_config("rollover", DEFAULT_ROLLOVER))

    def _today(self) -> int:
        """Unix timestamp (seconds) of today's day start, 00:00 UTC."""
        return int(
            self._col.db.scalar(
                "SELECT CAST(STRFTIME('%s', 'now', ?, 'localtime', 'start of day') AS int)",
                f"-{self._offset()} hours",
            )
        )

    def _deck_ids(self, current_deck_only: bool) -> list[int] | None:
        if not current_deck_only:
            return None
        decks = self._col.decks
        return list(decks.deck_and_child_ids(decks.get_current_id()))

    def _cards_done(self, current_deck_only: bool) -> list[Sequence[int]]:
        """Reviews per day, grouped in local time with the rollover applied.

        Manual reschedules (ease 0) are not reviews and are left out.
        """
        offset_secs = self._offset() * 3600
        where = ["ease >= 1"]
        dids = self._deck_ids(current_deck_only)
        if dids is not None:
            where.append(f"cid IN (SELECT id FROM cards WHERE did IN {ids2str(dids)})")
        return self._col.db.all(
            f"""
SELECT CAST(STRFTIME('%s', id / 1000 - {offset_secs}, 'unixepoch',
                     'localtime', 'start of day') AS int) AS day, COUNT()
FROM revlog WHERE {" AND ".join(where)}
GROUP BY day ORDER BY day"""
        )

    def _cards_due(
        self, start: int, stop: int, current_deck_only: bool
    ) -> list[Sequence[int]]:
        """Cards due per day from today on, as negative counts."""
        where = ["queue IN (2,3)"]
        dids = self._deck_ids(current_deck_only)
        if dids is not None:
            where.append(f"did IN {ids2str(dids)}")
        rows = self._col.db.all(
            f"""
SELECT
STRFTIME('%s', 'now', ?, 'localtime', 'start of day') + (due - ?) * 86400
AS day, -COUNT()
FROM cards
WHERE {" AND ".join(where)} AND day >= ? AND day < ?
GROUP BY day ORDER BY day""",
            f"-{self._offset()} hours",
            self._col.sched.today,
            start,
            stop,
        )
        return [(int(day), int(count)) for day, count in rows]


# Rendering
######################################################################

CSS_COLORS = (
    "rh-col0",
    "rh-col11",
    "rh-col12",
    "rh-col13",
    "rh-col14",
    "rh-col15",
    "rh-col16",
    "rh-col17",
    "rh-col18",
    "rh-col19",
    "rh-col20",
)

STREAK_LEVELS = list(
    zip((0, 14, 30, 90, 180, 365), [CSS_COLORS[i] for i in (0, 2, 4, 6, 9, 10)])
)
PERCENTAGE_LEVELS = list(zip((0, 25, 50, 60, 70, 80, 85, 90, 95, 99), CSS_COLORS))
DYNAMIC_LEGEND_FACTORS = (0.125, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0)

HEATMAP_MODE = {
    "domain": "year",
    "subdomain": "day",
    "range": 1,
    "domLabForm": "%Y",
}

HTML_MAIN = f"""
<script type="text/javascript" src="{WEB_BASE}/js/vendor/d3.min.js"></script>
<script type="text/javascript" src="{WEB_BASE}/js/vendor/anki-review-heatmap.js"></script>
<script>
var rhPlatform = "{PLATFORM}";
var rhNewFinderAPI = true;
</script>
<div class="rh-container {{classes}}">
{{content}}
</div>
"""

HTML_HEATMAP = f"""
<div class="heatmap">
    <div class="heatmap-controls">
        <div class="alignleft">
            <span>&nbsp;</span>
        </div>
        <div class="aligncenter">
            <div title="Go back\n(Shift-click for first year)" onclick="reviewHeatmap.onHmNavigate(event, this, 'prev');" class="hm-btn">
                <img height="10px" src="{WEB_BASE}/imgs/heatmap-left.svg" />
            </div>
            <div title="Today" onclick="reviewHeatmap.onHmHome(event, this);" class="hm-btn">
                <img height="10px" src="{WEB_BASE}/imgs/heatmap-circle.svg" />
            </div>
            <div title="Go forward\n(Shift-click for last year)" onclick="reviewHeatmap.onHmNavigate(event, this, 'next');" class="hm-btn">
                <img height="10px" src="{WEB_BASE}/imgs/heatmap-right.svg" />
            </div>
        </div>
        <div class="alignright">
            <span>&nbsp;</span>
        </div>
        <div style="clear: both;">&nbsp;</div>
    </div>
    <div id="cal-heatmap"></div>
</div>
<script type="text/javascript">
    window.reviewHeatmap = new ReviewHeatmap({{options}});
    reviewHeatmap.create({{data}});
</script>
"""

HTML_STREAK = """
<div class="streak">
    <span class="streak-info">Daily average:</span>
    <span title="Average reviews on active days"
        class="sstats {class_activity_daily_avg}">{text_activity_daily_avg}</span>
    <span class="streak-info">Days learned:</span>
    <span title="Percentage of days with review activity over entire review history"
        class="sstats {class_pct_days_active}">{text_pct_days_active}%</span>
    <span class="streak-info">Longest streak:</span>
    <span title="Longest continuous streak of review activity. All types of repetitions included."
        class="sstats {class_streak_max}">{text_streak_max}</span>
    <span class="streak-info">Current streak:</span>
    <span title="Current card review activity streak. All types of repetitions included."
        class="sstats {class_streak_cur}">{text_streak_cur}</span>
</div>
"""

HTML_NODATA = "No review activity to show yet."


def _dynamic_legend(average: int) -> list[float]:
    avg = max(20, average)
    return [factor * avg for factor in DYNAMIC_LEGEND_FACTORS]


def _heatmap_legend(legend: list[float]) -> list[float]:
    # negative mirror for future (due) days, so the calendar can colour the
    # forecast differently without changes to cal-heatmap
    return [-i for i in legend[::-1]] + [0.0] + legend


def _level_class(value: int, levels: Sequence[tuple[float, str]]) -> str:
    css_class = CSS_COLORS[0]
    for threshold, css_class in levels:
        if value <= threshold:
            break
    return css_class


def _pluralize(count: int, term: str) -> str:
    return f"{count} {term}{'s' if abs(count) != 1 else ''}"


def render_stats(stats: HeatmapStats) -> str:
    dynamic_levels = list(zip(_dynamic_legend(stats.activity_daily_avg), CSS_COLORS))
    return HTML_STREAK.format(
        class_activity_daily_avg=_level_class(stats.activity_daily_avg, dynamic_levels),
        text_activity_daily_avg=_pluralize(stats.activity_daily_avg, "card"),
        class_pct_days_active=_level_class(stats.pct_days_active, PERCENTAGE_LEVELS),
        text_pct_days_active=str(stats.pct_days_active),
        class_streak_max=_level_class(stats.streak_max, STREAK_LEVELS),
        text_streak_max=_pluralize(stats.streak_max, "day"),
        class_streak_cur=_level_class(stats.streak_cur, STREAK_LEVELS),
        text_streak_cur=_pluralize(stats.streak_cur, "day"),
    )


def render_heatmap(report: ActivityReport, current_deck_only: bool) -> str:
    options = {
        **HEATMAP_MODE,
        "start": report.start,
        "stop": report.stop,
        "today": report.today,
        "offset": report.offset,
        "legend": _heatmap_legend(_dynamic_legend(report.stats.activity_daily_avg)),
        "whole": not current_deck_only,
    }
    return HTML_HEATMAP.format(
        options=json.dumps(options), data=json.dumps(report.activity)
    )


def render_report(
    report: ActivityReport | None, view: HeatmapView, current_deck_only: bool
) -> str:
    classes = f"rh-platform-{PLATFORM} rh-theme-lime rh-mode-year rh-view-{view.value}"
    if report is None:
        return HTML_MAIN.format(content=HTML_NODATA, classes=classes)
    content = render_heatmap(report, current_deck_only) + render_stats(report.stats)
    return HTML_MAIN.format(content=content, classes=classes)


# Integration
######################################################################


@dataclass(frozen=True)
class _RenderCache:
    html: str
    view: HeatmapView
    current_deck_only: bool
    deck_id: int
    col_mod: int


class ReviewHeatmap:
    """Draws the heatmap into the deck list and the overview, and handles
    the calendar's clicks."""

    def __init__(self, mw: AnkiQt) -> None:
        self.mw = mw
        self._cache: _RenderCache | None = None

    def enabled(self) -> bool:
        col = self.mw.col
        if col is None:
            return False
        return col.get_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED)

    def render(self, view: HeatmapView, current_deck_only: bool) -> str:
        col = self.mw.col
        if col is None or not self.enabled():
            return ""
        deck_id = int(col.decks.get_current_id()) if current_deck_only else 0
        col_mod = col.mod
        cache = self._cache
        if (
            cache is not None
            and cache.view == view
            and cache.current_deck_only == current_deck_only
            and cache.deck_id == deck_id
            and cache.col_mod == col_mod
        ):
            return cache.html
        report = ActivityReporter(col).get_report(current_deck_only)
        html = render_report(report, view, current_deck_only)
        self._cache = _RenderCache(html, view, current_deck_only, deck_id, col_mod)
        return html

    # hooks

    def on_deck_browser_will_render_content(
        self, deck_browser: DeckBrowser, content: DeckBrowserContent
    ) -> None:
        content.stats += self.render(HeatmapView.deckbrowser, current_deck_only=False)

    def on_overview_will_render_content(
        self, overview: Overview, content: OverviewContent
    ) -> None:
        content.table += self.render(HeatmapView.overview, current_deck_only=True)

    def on_webview_did_receive_js_message(
        self, handled: tuple[bool, Any], message: str, context: Any
    ) -> tuple[bool, Any]:
        if not message.startswith("revhm_"):
            return handled
        command, _, payload = message[len("revhm_") :].partition(":")
        if command == "browse" and payload:
            self.open_browser(payload)
        # the add-on's other commands (mode/theme switch, options, contrib)
        # have no counterpart here
        return (True, None)

    def open_browser(self, search: str) -> None:
        import aqt

        aqt.dialogs.open("Browser", self.mw, search=(search,))


_instance: ReviewHeatmap | None = None


def initialize(mw: AnkiQt) -> ReviewHeatmap:
    global _instance
    heatmap = ReviewHeatmap(mw)
    gui_hooks.deck_browser_will_render_content.append(
        heatmap.on_deck_browser_will_render_content
    )
    gui_hooks.overview_will_render_content.append(
        heatmap.on_overview_will_render_content
    )
    gui_hooks.webview_did_receive_js_message.append(
        heatmap.on_webview_did_receive_js_message
    )
    _instance = heatmap
    return heatmap

# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The review heatmap on the deck list, the deck overview and the legacy
stats screen.

Ported into Clanki from Glutanimate's "Review Heatmap" add-on
(https://github.com/glutanimate/review-heatmap, GNU AGPLv3), settings
included; they live in Preferences > Review Heatmap (spec
ui.review-heatmap). The calendar itself is drawn by the add-on's JS bundle
(cal-heatmap on d3), vendored under qt/aqt/data/web/js/vendor/.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Sequence

from anki.collection import Config
from anki.decks import DeckId
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

# the collection config key holding the settings
CONFIG_KEY = "reviewHeatmap"
# where the add-on kept its settings: collection config and profile
ADDON_CONFIG_KEY = "heatmap"
# the add-on's folders (AnkiWeb ids of its versions, and a source install)
ADDON_DIRS = ("1771074083", "1722241614", "723520343", "review_heatmap")
# pm.meta flag: the "add-on disabled" notice was shown
ADDON_NOTICE_SHOWN_KEY = "reviewHeatmapAddonNoticeShown"

COLOR_SCHEMES: dict[str, str] = {
    "lime": "Lime",
    "olive": "Olive",
    "ice": "Ice",
    "magenta": "Magenta",
    "flame": "Flame",
}

CALENDAR_MODES: dict[str, dict[str, Any]] = {
    "year": {
        "label": "Yearly overview",
        "domain": "year",
        "subdomain": "day",
        "range": 1,
        "domLabForm": "%Y",
    },
    "months": {
        "label": "Continuous timeline",
        "domain": "month",
        "subdomain": "day",
        "range": 9,
        "domLabForm": "%b '%y",
    },
}


class HeatmapView(Enum):
    deckbrowser = "deckbrowser"
    overview = "overview"
    stats = "stats"


# Settings
######################################################################


@dataclass(frozen=True)
class HeatmapSettings:
    """Everything the add-on's options dialog offered (spec ui.review-heatmap).

    Whether the heatmap is drawn at all is the separate collection flag
    `reviewHeatmapEnabled`.
    """

    colors: str = "magenta"
    mode: str = "year"
    # history shown on the deck list and overview: days back, 0 = no limit
    history_limit_days: int = 0
    # ignore reviews before this day (unix seconds), 0 = none
    ignore_before: int = 0
    # forecast shown on the deck list and overview: days ahead, 0 = no limit
    forecast_limit_days: int = 0
    exclude_deleted_cards: bool = False
    exclude_manual_reschedules: bool = True
    # decks (with their subdecks) left out of the deck-list heatmap
    excluded_decks: tuple[int, ...] = field(default_factory=tuple)
    show_on_deck_list: bool = True
    show_on_overview: bool = True
    show_on_stats: bool = True
    # show the four figures even where the calendar is hidden
    streak_stats_always: bool = True

    def to_config(self) -> dict[str, Any]:
        data = asdict(self)
        data["excluded_decks"] = list(self.excluded_decks)
        return data

    @classmethod
    def from_config(cls, data: object) -> HeatmapSettings:
        """Settings from stored JSON; anything missing or invalid is the default."""

        if not isinstance(data, Mapping):
            return cls()
        default = cls()
        values: dict[str, Any] = {}
        for name, default_value in asdict(default).items():
            value = data.get(name, default_value)
            if name == "excluded_decks":
                if isinstance(value, (list, tuple)):
                    values[name] = tuple(
                        int(did)
                        for did in value
                        if isinstance(did, int) and not isinstance(did, bool)
                    )
                continue
            if isinstance(default_value, bool):
                if isinstance(value, bool):
                    values[name] = value
            elif isinstance(default_value, int):
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    values[name] = value
            elif isinstance(value, str):
                values[name] = value
        settings = replace(default, **values)
        if settings.colors not in COLOR_SCHEMES:
            settings = replace(settings, colors=default.colors)
        if settings.mode not in CALENDAR_MODES:
            settings = replace(settings, mode=default.mode)
        return settings

    def shows(self, view: HeatmapView) -> bool:
        return {
            HeatmapView.deckbrowser: self.show_on_deck_list,
            HeatmapView.overview: self.show_on_overview,
            HeatmapView.stats: self.show_on_stats,
        }[view]


def settings_from_addon(synced: object, profile: object) -> HeatmapSettings:
    """The add-on's stored settings in Clanki's terms.

    A colour scheme left at the add-on's own default (lime) is not carried
    over, so such a user gets Clanki's default (magenta).
    """

    synced = synced if isinstance(synced, Mapping) else {}
    profile = profile if isinstance(profile, Mapping) else {}
    display = profile.get("display")
    display = display if isinstance(display, Mapping) else {}
    data: dict[str, Any] = {
        "mode": synced.get("mode"),
        "history_limit_days": synced.get("limhist"),
        "ignore_before": synced.get("limdate"),
        "forecast_limit_days": synced.get("limfcst"),
        "exclude_deleted_cards": synced.get("limcdel"),
        "exclude_manual_reschedules": synced.get("limresched"),
        "excluded_decks": synced.get("limdecks"),
        "show_on_deck_list": display.get("deckbrowser"),
        "show_on_overview": display.get("overview"),
        "show_on_stats": display.get("stats"),
        "streak_stats_always": profile.get("statsvis"),
    }
    if synced.get("colors") != "lime":
        data["colors"] = synced.get("colors")
    return HeatmapSettings.from_config(
        {key: value for key, value in data.items() if value is not None}
    )


def load_settings(col: Collection, profile: object = None) -> HeatmapSettings:
    """The stored settings; until Clanki's are saved once, the add-on's."""

    stored = col.get_config(CONFIG_KEY, None)
    if stored is not None:
        return HeatmapSettings.from_config(stored)
    addon_synced = col.get_config(ADDON_CONFIG_KEY, None)
    addon_profile = (
        profile.get(ADDON_CONFIG_KEY) if isinstance(profile, Mapping) else None
    )
    if addon_synced is None and addon_profile is None:
        return HeatmapSettings()
    return settings_from_addon(addon_synced, addon_profile)


def save_settings(col: Collection, settings: HeatmapSettings) -> None:
    col.set_config(CONFIG_KEY, settings.to_config())


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

    def __init__(self, col: Collection, settings: HeatmapSettings) -> None:
        self._col = col
        self._settings = settings

    def get_report(
        self,
        current_deck_only: bool,
        history_days: int | None = None,
        forecast_days: int | None = None,
    ) -> ActivityReport | None:
        """`history_days` / `forecast_days` override the settings' limits
        (the stats screen passes its period); None means use the settings."""

        today = self._today()
        history_start = (
            self._days_from_today(today, -history_days)
            if history_days is not None
            else self._settings_history_start(today)
        )
        if forecast_days is not None:
            forecast_stop = self._days_from_today(today, forecast_days)
        else:
            forecast_stop = self._days_from_today(
                today, self._settings.forecast_limit_days or MAX_FORECAST_DAYS
            )
        history = self._cards_done(current_deck_only, history_start)
        if not history:
            return None
        forecast = self._cards_due(
            start=today, stop=forecast_stop, current_deck_only=current_deck_only
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

    def _day_start(self, timestamp: int) -> int:
        """Start of the local day holding `timestamp`, as the day keys are."""
        return int(
            self._col.db.scalar(
                "SELECT CAST(STRFTIME('%s', ?, 'unixepoch', 'localtime', 'start of day') AS int)",
                timestamp,
            )
        )

    @staticmethod
    def _days_from_today(today: int, days: int) -> int:
        return today + 86400 * days

    def _settings_history_start(self, today: int) -> int | None:
        limits = []
        if self._settings.history_limit_days:
            limits.append(
                self._days_from_today(today, -self._settings.history_limit_days)
            )
        if self._settings.ignore_before:
            limits.append(self._day_start(self._settings.ignore_before))
        return max(limits) if limits else None

    def _deck_ids(self, current_deck_only: bool) -> list[int] | None:
        """The decks counted, or None for all of them."""

        decks = self._col.decks
        if current_deck_only:
            return list(decks.deck_and_child_ids(decks.get_current_id()))
        if not self._settings.excluded_decks:
            return None
        excluded: set[int] = set()
        for did in self._settings.excluded_decks:
            if decks.name_if_exists(DeckId(did)) is None:
                continue
            excluded.update(
                int(child) for child in decks.deck_and_child_ids(DeckId(did))
            )
        return [
            int(deck.id)
            for deck in decks.all_names_and_ids()
            if deck.id not in excluded
        ]

    def _cards_done(
        self, current_deck_only: bool, start: int | None
    ) -> list[Sequence[int]]:
        """Reviews per day, grouped in local time with the rollover applied."""

        offset_secs = self._offset() * 3600
        where = []
        if start is not None:
            where.append(f"day >= {int(start)}")
        if self._settings.exclude_manual_reschedules:
            where.append("ease >= 1")
        dids = self._deck_ids(current_deck_only)
        if dids is not None:
            where.append(f"cid IN (SELECT id FROM cards WHERE did IN {ids2str(dids)})")
        elif self._settings.exclude_deleted_cards:
            where.append("cid IN (SELECT id FROM cards)")
        condition = f"WHERE {' AND '.join(where)}" if where else ""
        return self._col.db.all(
            f"""
SELECT CAST(STRFTIME('%s', id / 1000 - {offset_secs}, 'unixepoch',
                     'localtime', 'start of day') AS int) AS day, COUNT()
FROM revlog {condition}
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

# The add-on's styles size the settings button differently from the three
# navigation buttons; all four get one width.
HEATMAP_BUTTON_CSS = """
<style>
.heatmap .heatmap-controls .hm-btn {
    box-sizing: border-box;
    width: 28px;
    padding: 2px 0;
    text-align: center;
}
.heatmap .heatmap-controls .hm-btn > img {
    height: 10px;
    width: 10px;
    object-fit: contain;
}
</style>
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
            <div title="Today\n(Shift-click to switch the calendar mode)" onclick="reviewHeatmap.onHmHome(event, this);" class="hm-btn">
                <img height="10px" src="{WEB_BASE}/imgs/heatmap-circle.svg" />
            </div>
            <div title="Go forward\n(Shift-click for last year)" onclick="reviewHeatmap.onHmNavigate(event, this, 'next');" class="hm-btn">
                <img height="10px" src="{WEB_BASE}/imgs/heatmap-right.svg" />
            </div>
        </div>
        <div class="alignright">
            <div class="hm-btn opts-btn" title="Settings\n(Shift-click to switch the colours)" onclick="reviewHeatmap.onHmOpts(event, this);">
                <img src="{WEB_BASE}/imgs/heatmap-options.svg" />
            </div>
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

HTML_NODATA = """
No review activity to show yet
(<span class="linkspan" onclick='pycmd("revhm_opts");'>settings</span>).
"""


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


def render_heatmap(
    report: ActivityReport, current_deck_only: bool, settings: HeatmapSettings
) -> str:
    mode = CALENDAR_MODES[settings.mode]
    options = {
        "domain": mode["domain"],
        "subdomain": mode["subdomain"],
        "range": mode["range"],
        "domLabForm": mode["domLabForm"],
        "start": report.start,
        "stop": report.stop,
        "today": report.today,
        "offset": report.offset,
        "legend": _heatmap_legend(_dynamic_legend(report.stats.activity_daily_avg)),
        "whole": not current_deck_only,
    }
    return HEATMAP_BUTTON_CSS + HTML_HEATMAP.format(
        options=json.dumps(options), data=json.dumps(report.activity)
    )


def render_report(
    report: ActivityReport | None,
    view: HeatmapView,
    current_deck_only: bool,
    settings: HeatmapSettings = HeatmapSettings(),
) -> str:
    classes = [
        f"rh-platform-{PLATFORM}",
        f"rh-theme-{settings.colors}",
        f"rh-mode-{settings.mode}",
        f"rh-view-{view.value}",
    ]
    if report is None:
        return HTML_MAIN.format(content=HTML_NODATA, classes=" ".join(classes))
    content = ""
    if settings.shows(view):
        content += render_heatmap(report, current_deck_only, settings)
    else:
        classes.append("rh-disable-heatmap")
    content += render_stats(report.stats)
    return HTML_MAIN.format(content=content, classes=" ".join(classes))


# Integration
######################################################################


@dataclass(frozen=True)
class _RenderCache:
    html: str
    key: tuple[Any, ...]


class ReviewHeatmap:
    """Draws the heatmap into the deck list, the overview and the legacy
    stats screen, and handles the calendar's clicks."""

    def __init__(self, mw: AnkiQt) -> None:
        self.mw = mw
        self._cache: _RenderCache | None = None

    def enabled(self) -> bool:
        col = self.mw.col
        if col is None:
            return False
        return col.get_config_bool(Config.Bool.REVIEW_HEATMAP_ENABLED)

    def settings(self) -> HeatmapSettings:
        col = self.mw.col
        if col is None:
            return HeatmapSettings()
        pm = getattr(self.mw, "pm", None)
        return load_settings(col, getattr(pm, "profile", None))

    def render(
        self,
        view: HeatmapView,
        current_deck_only: bool,
        history_days: int | None = None,
        forecast_days: int | None = None,
    ) -> str:
        col = self.mw.col
        if col is None or not self.enabled():
            return ""
        settings = self.settings()
        if not settings.shows(view) and not settings.streak_stats_always:
            return ""
        deck_id = int(col.decks.get_current_id()) if current_deck_only else 0
        key = (
            view,
            current_deck_only,
            deck_id,
            col.mod,
            history_days,
            forecast_days,
            settings,
        )
        if self._cache is not None and self._cache.key == key:
            return self._cache.html
        report = ActivityReporter(col, settings).get_report(
            current_deck_only, history_days, forecast_days
        )
        html = render_report(report, view, current_deck_only, settings)
        self._cache = _RenderCache(html, key)
        return html

    def render_for_stats(self, period: int, whole_collection: bool) -> str:
        """The legacy stats screen: 1 month, 1 year or the whole history."""

        days = {0: 31, 1: 365}.get(period)
        return self.render(
            HeatmapView.stats,
            current_deck_only=not whole_collection,
            history_days=days,
            forecast_days=days,
        )

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
        elif command == "opts":
            self.open_settings()
        elif command == "modeswitch":
            self._cycle_setting("mode", list(CALENDAR_MODES), context)
        elif command == "themeswitch":
            self._cycle_setting("colors", list(COLOR_SCHEMES), context)
        # the add-on's contribution links are not ported
        return (True, None)

    def open_browser(self, search: str) -> None:
        import aqt

        aqt.dialogs.open("Browser", self.mw, search=(search,))

    def open_settings(self) -> None:
        """Preferences, on the Review Heatmap tab."""

        import aqt

        preferences = aqt.dialogs.open("Preferences", self.mw)
        show_tab = getattr(preferences, "show_review_heatmap_tab", None)
        if callable(show_tab):
            show_tab()

    def _cycle_setting(self, name: str, values: list[str], context: Any) -> None:
        col = self.mw.col
        if col is None:
            return
        settings = self.settings()
        current = getattr(settings, name)
        following = values[(values.index(current) + 1) % len(values)]
        changes: dict[str, Any] = {name: following}
        save_settings(col, replace(settings, **changes))
        self.redraw(context)

    def redraw_current_screen(self) -> None:
        state = getattr(self.mw, "state", None)
        if state == "deckBrowser":
            self.redraw(self.mw.deckBrowser)
        elif state == "overview":
            self.redraw(self.mw.overview)

    def redraw(self, context: Any) -> None:
        """Draw the screen showing the heatmap again, without recomputing
        the deck list's due counts."""

        from aqt.deckbrowser import DeckBrowser
        from aqt.overview import Overview

        if isinstance(context, DeckBrowser):
            context._renderPage(reuse=True)
        elif isinstance(context, Overview):
            context.refresh()
        else:
            refresh = getattr(context, "refresh", None)
            if callable(refresh):
                refresh()


def disable_review_heatmap_addon(addon_manager: object) -> list[str]:
    """Disable an installed, enabled Review Heatmap add-on (Clanki draws the
    heatmap itself, and both would show). Returns the folders disabled."""

    all_addons = getattr(addon_manager, "allAddons", None)
    is_enabled = getattr(addon_manager, "isEnabled", None)
    toggle = getattr(addon_manager, "toggleEnabled", None)
    if not (callable(all_addons) and callable(is_enabled) and callable(toggle)):
        return []
    disabled = []
    for folder in all_addons():
        if folder in ADDON_DIRS and is_enabled(folder):
            toggle(folder, enable=False)
            disabled.append(folder)
    return disabled


_instance: ReviewHeatmap | None = None


def instance() -> ReviewHeatmap | None:
    return _instance


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

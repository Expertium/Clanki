# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Review Heatmap tab of Preferences (spec ui.review-heatmap): the
settings of the former add-on's options dialog, plus the on/off switch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDate,
    QDateEdit,
    QDateTime,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    Qt,
    QTime,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.review_heatmap import (
    CALENDAR_MODES,
    COLOR_SCHEMES,
    MAX_FORECAST_DAYS,
    HeatmapSettings,
    load_settings,
    save_settings,
)
from aqt.utils import tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt

MAX_LIMIT_DAYS = 36500


class ReviewHeatmapPreferences(QWidget):
    def __init__(
        self, mw: AnkiQt, enabled: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.mw = mw
        assert mw.col is not None
        self._initial = load_settings(mw.col, getattr(mw.pm, "profile", None))
        settings = self._initial

        layout = QVBoxLayout(self)
        self.enabled_box = QCheckBox(tr.preferences_show_review_heatmap())
        self.enabled_box.setChecked(enabled)
        layout.addWidget(self.enabled_box)

        # appearance
        self.appearance = QGroupBox(tr.preferences_heatmap_appearance())
        form = QFormLayout(self.appearance)
        self.colors = QComboBox()
        for key, label in COLOR_SCHEMES.items():
            self.colors.addItem(label, key)
        self.colors.setCurrentIndex(list(COLOR_SCHEMES).index(settings.colors))
        form.addRow(tr.preferences_heatmap_color_scheme(), self.colors)
        self.mode = QComboBox()
        for key, mode in CALENDAR_MODES.items():
            self.mode.addItem(str(mode["label"]), key)
        self.mode.setCurrentIndex(list(CALENDAR_MODES).index(settings.mode))
        form.addRow(tr.preferences_heatmap_calendar_mode(), self.mode)
        layout.addWidget(self.appearance)

        # where it shows
        self.visibility = QGroupBox(tr.preferences_heatmap_visibility())
        box = QVBoxLayout(self.visibility)
        self.on_deck_list = QCheckBox(tr.preferences_heatmap_on_deck_list())
        self.on_deck_list.setChecked(settings.show_on_deck_list)
        self.on_overview = QCheckBox(tr.preferences_heatmap_on_overview())
        self.on_overview.setChecked(settings.show_on_overview)
        self.on_stats = QCheckBox(tr.preferences_heatmap_on_stats())
        self.on_stats.setChecked(settings.show_on_stats)
        self.streak_always = QCheckBox(tr.preferences_heatmap_streak_always())
        self.streak_always.setChecked(settings.streak_stats_always)
        for widget in (
            self.on_deck_list,
            self.on_overview,
            self.on_stats,
            self.streak_always,
        ):
            box.addWidget(widget)
        layout.addWidget(self.visibility)

        # which reviews count
        self.history = QGroupBox(tr.preferences_heatmap_history())
        self.history.setToolTip(tr.preferences_heatmap_limits_tooltip())
        form = QFormLayout(self.history)
        self.history_limit = self._days_spin_box(settings.history_limit_days)
        form.addRow(tr.preferences_heatmap_history_limit(), self.history_limit)
        self.forecast_limit = self._days_spin_box(
            settings.forecast_limit_days,
            maximum=MAX_FORECAST_DAYS,
            unlimited_text=tr.preferences_heatmap_forecast_five_years(),
        )
        form.addRow(tr.preferences_heatmap_forecast_limit(), self.forecast_limit)
        self.ignore_before_box = QCheckBox(tr.preferences_heatmap_ignore_before())
        self.ignore_before = QDateEdit()
        self.ignore_before.setCalendarPopup(True)
        self.ignore_before.setMaximumDate(QDate.currentDate())
        crt = getattr(mw.col, "crt", 0) or 0
        if crt:
            self.ignore_before.setMinimumDate(QDateTime.fromSecsSinceEpoch(crt).date())
        if settings.ignore_before:
            self.ignore_before_box.setChecked(True)
            self.ignore_before.setDate(
                QDateTime.fromSecsSinceEpoch(settings.ignore_before).date()
            )
        else:
            self.ignore_before.setDate(QDate.currentDate())
        self.ignore_before.setEnabled(self.ignore_before_box.isChecked())
        qconnect(self.ignore_before_box.toggled, self.ignore_before.setEnabled)
        form.addRow(self.ignore_before_box, self.ignore_before)
        self.exclude_deleted = QCheckBox(tr.preferences_heatmap_exclude_deleted())
        self.exclude_deleted.setChecked(settings.exclude_deleted_cards)
        form.addRow(self.exclude_deleted)
        self.exclude_rescheduled = QCheckBox(
            tr.preferences_heatmap_exclude_rescheduled()
        )
        self.exclude_rescheduled.setChecked(settings.exclude_manual_reschedules)
        form.addRow(self.exclude_rescheduled)
        layout.addWidget(self.history)

        # decks left out of the main-screen heatmap
        self.decks = QGroupBox(tr.preferences_heatmap_excluded_decks())
        self.decks.setToolTip(tr.preferences_heatmap_excluded_decks_tooltip())
        row = QHBoxLayout(self.decks)
        self.deck_list = QListWidget()
        self.deck_list.setSortingEnabled(True)
        for did in settings.excluded_decks:
            name = mw.col.decks.name_if_exists(did)  # type: ignore[arg-type]
            if name:
                self._add_deck_item(name, did)
        row.addWidget(self.deck_list)
        buttons = QVBoxLayout()
        self.add_deck = QPushButton(tr.preferences_heatmap_add_deck())
        qconnect(self.add_deck.clicked, self._on_add_deck)
        self.remove_deck = QPushButton(tr.preferences_heatmap_remove_deck())
        qconnect(self.remove_deck.clicked, self._on_remove_deck)
        buttons.addWidget(self.add_deck)
        buttons.addWidget(self.remove_deck)
        buttons.addStretch()
        row.addLayout(buttons)
        layout.addWidget(self.decks)
        layout.addStretch()

        qconnect(self.enabled_box.toggled, self._sync_enabled)
        self._sync_enabled(self.enabled_box.isChecked())

    @staticmethod
    def _days_spin_box(
        value: int,
        maximum: int = MAX_LIMIT_DAYS,
        unlimited_text: str | None = None,
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setSpecialValueText(unlimited_text or tr.preferences_heatmap_no_limit())
        spin.setSuffix(f" {tr.preferences_heatmap_days()}")
        spin.setValue(value)
        return spin

    def _sync_enabled(self, enabled: bool) -> None:
        for group in (self.appearance, self.visibility, self.history, self.decks):
            group.setEnabled(enabled)

    def _add_deck_item(self, name: str, did: int) -> None:
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, int(did))
        self.deck_list.addItem(item)

    def _excluded_deck_ids(self) -> list[int]:
        return [
            int(self.deck_list.item(row).data(Qt.ItemDataRole.UserRole))  # type: ignore[union-attr]
            for row in range(self.deck_list.count())
        ]

    def _on_add_deck(self) -> None:
        from aqt.studydeck import StudyDeck

        def chosen(study_deck: StudyDeck) -> None:
            name = study_deck.name
            col = self.mw.col
            if not name or col is None:
                return
            did = col.decks.id_for_name(name)
            if did is None or int(did) in self._excluded_deck_ids():
                return
            self._add_deck_item(name, int(did))

        StudyDeck(
            self.mw,
            accept=tr.actions_choose(),
            title=tr.decks_choose_deck(),
            parent=self,
            geomKey="selectDeck",
            callback=chosen,
        )

    def _on_remove_deck(self) -> None:
        for item in self.deck_list.selectedItems():
            self.deck_list.takeItem(self.deck_list.row(item))

    # reading back

    def is_enabled(self) -> bool:
        return self.enabled_box.isChecked()

    def settings(self) -> HeatmapSettings:
        ignore_before = 0
        if self.ignore_before_box.isChecked():
            ignore_before = QDateTime(
                self.ignore_before.date(), QTime(0, 0)
            ).toSecsSinceEpoch()
        return HeatmapSettings(
            colors=str(self.colors.currentData()),
            mode=str(self.mode.currentData()),
            history_limit_days=self.history_limit.value(),
            ignore_before=ignore_before,
            forecast_limit_days=self.forecast_limit.value(),
            exclude_deleted_cards=self.exclude_deleted.isChecked(),
            exclude_manual_reschedules=self.exclude_rescheduled.isChecked(),
            excluded_decks=tuple(self._excluded_deck_ids()),
            show_on_deck_list=self.on_deck_list.isChecked(),
            show_on_overview=self.on_overview.isChecked(),
            show_on_stats=self.on_stats.isChecked(),
            streak_stats_always=self.streak_always.isChecked(),
        )

    def save(self) -> bool:
        """Store the settings when they changed; True if they did."""

        settings = self.settings()
        col = self.mw.col
        if col is None or settings == self._initial:
            return False
        save_settings(col, settings)
        self._initial = settings
        return True

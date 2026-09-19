# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto

import aqt
import aqt.browser
import aqt.gui_hooks
from aqt.qt import *
from aqt.theme import theme_manager
from aqt.utils import tr


class SidebarTool(Enum):
    SELECT = auto()
    SEARCH = auto()


class SidebarToolbar(QToolBar):
    _tools: tuple[tuple[SidebarTool, str, Callable[[], str]], ...] = (
        (
            SidebarTool.SEARCH,
            "mdi:magnify",
            tr.actions_search,
        ),
        (
            SidebarTool.SELECT,
            "mdi:selection-drag",
            tr.actions_select,
        ),
    )

    def __init__(self, sidebar: aqt.browser.sidebar.SidebarTreeView) -> None:
        super().__init__()
        self.sidebar = sidebar
        self._action_group = QActionGroup(self)
        qconnect(self._action_group.triggered, self._on_action_group_triggered)
        self._setup_tools()
        self.setIconSize(QSize(18, 18))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyle(QStyleFactory.create("fusion"))
        aqt.gui_hooks.theme_did_change.append(self._update_icons)

    def _setup_tools(self) -> None:
        for row, tool in enumerate(self._tools):
            action = self.addAction(
                theme_manager.icon_from_resources(tool[1]), tool[2]()
            )
            assert action is not None
            action.setCheckable(True)
            action.setShortcut(f"Alt+{row + 1}")
            self._action_group.addAction(action)
        # always start with first tool
        active = 0
        self._action_group.actions()[active].setChecked(True)
        self.sidebar.tool = self._tools[active][0]

    def _on_action_group_triggered(self, action: QAction) -> None:
        index = self._action_group.actions().index(action)
        self.sidebar.tool = self._tools[index][0]

    def apply_ui_mode(self, show_select: bool) -> None:
        """The Select tool shows when the split says so (by default Advanced
        mode only); hidden, its button is dropped but its shortcut kept,
        which the Browser window holds (spec ui.browser-simple-view)."""
        search, select = self._action_group.actions()
        if show_select:
            if select not in self.actions():
                self.addAction(select)
            return
        if select.isChecked():
            search.trigger()
        self.removeAction(select)

    def cleanup(self) -> None:
        aqt.gui_hooks.theme_did_change.remove(self._update_icons)

    def _update_icons(self) -> None:
        for idx, action in enumerate(self._action_group.actions()):
            action.setIcon(theme_manager.icon_from_resources(self._tools[idx][1]))

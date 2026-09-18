# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Help > Support Anki & Clanki (spec branding.support-window).

Replaces what used to be a direct link to Anki's own "support Anki" page
with a small dialog: a fork clarification consistent with the About window,
a link to Anki's own manual (labelled as Anki's, not Clanki's), and Andrew's
Ethereum address for anyone who wants to support Clanki's author
specifically. The two destinations are kept visually apart so nobody
mistakes support for Andrew as support for Anki, or the reverse.
"""

from collections.abc import Callable

import aqt
from aqt.qt import *
from aqt.utils import disable_help_button, tooltip, tr

# Anki's own manual, not Clanki's (spec branding.support-window): Clanki has
# no manual of its own yet, and linking here would be misread as Clanki
# documentation if left unlabelled.
ANKI_MANUAL_URL = "https://docs.ankiweb.net"

# Andrew's Ethereum address, EIP-55 checksummed: the mixed upper/lower-case
# letters ARE the checksum. Do not lowercase, reformat, or otherwise
# transform this string -- a single wrong character sends a stranger's
# money to nobody. Pasted in exactly once and pinned by
# qt/tests/test_support.py; verified format (40 hex chars after 0x, mixed
# case) but the EIP-55 checksum itself was not independently verified
# (Andrew, 2026-09-18: no keccak library was available to check it).
CLANKI_AUTHOR_ETHEREUM_ADDRESS = "0xf9c22186be4dbF21dE3E149C167Ed90DE0E37279"


class ClosableQDialog(QDialog):
    def reject(self) -> None:
        aqt.dialogs.markClosed("Support")
        QDialog.reject(self)

    def accept(self) -> None:
        aqt.dialogs.markClosed("Support")
        QDialog.accept(self)

    def closeWithCallback(self, callback: Callable[[], None]) -> None:
        self.reject()
        callback()


def _support_html() -> str:
    """The Support window's content. Pure function, no Qt widget
    dependency, so the exact address string can be pinned directly (spec
    branding.support-window)."""
    return (
        f"<p><b>{tr.support_clanki_fork_notice()}</b></p>"
        f"<p>{tr.support_anki_manual_link(val=ANKI_MANUAL_URL)}</p>"
        "<hr>"
        f"<p><b>{tr.support_clanki_heading()}</b></p>"
        f"<p>{tr.support_clanki_ethereum_label()} "
        f"<code>{CLANKI_AUTHOR_ETHEREUM_ADDRESS}</code></p>"
    )


def show(mw: aqt.AnkiQt) -> QDialog:
    dialog = ClosableQDialog(mw)
    dialog.setWindowTitle(tr.support_window_title())
    disable_help_button(dialog)
    mw.garbage_collect_on_dialog_finish(dialog)

    layout = QVBoxLayout()

    label = QLabel()
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(_support_html())
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextBrowserInteraction
        | Qt.TextInteractionFlag.TextSelectableByMouse
    )
    label.setWordWrap(True)
    layout.addWidget(label)

    def on_copy() -> None:
        clipboard = QApplication.clipboard()
        assert clipboard is not None
        clipboard.setText(CLANKI_AUTHOR_ETHEREUM_ADDRESS)
        tooltip(tr.support_address_copied_to_clipboard(), parent=dialog)

    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    copy_button = QPushButton(tr.support_copy_address())
    qconnect(copy_button.clicked, on_copy)
    button_box.addButton(copy_button, QDialogButtonBox.ButtonRole.ActionRole)
    qconnect(button_box.accepted, dialog.accept)
    layout.addWidget(button_box)

    dialog.setLayout(layout)
    dialog.setMinimumWidth(420)
    dialog.show()
    return dialog

# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The word "retrievability" explains itself on hover wherever a Qt screen
shows it (spec ui.retrievability-advanced-only): underlined in a label, with
"probability of recall" as the tooltip. The screens that show it are
Advanced-only. The web pages do the same with RetrievabilityText.svelte."""

from __future__ import annotations

import html
import re

from aqt.qt import QLabel, Qt
from aqt.utils import tr


def explanation() -> str:
    """What the hover says."""
    return tr.card_stats_retrievability_explanation()


def _term_pattern() -> re.Pattern[str] | None:
    term = tr.card_stats_retrievability_term()
    return re.compile(re.escape(term), re.IGNORECASE) if term else None


def mentions_retrievability(text: str) -> bool:
    pattern = _term_pattern()
    return bool(pattern and pattern.search(text))


def underlined_html(text: str) -> str:
    """`text` as rich text, every occurrence of the word underlined."""
    escaped = html.escape(text)
    pattern = _term_pattern()
    if pattern is None:
        return escaped
    return pattern.sub(lambda match: f"<u>{match.group(0)}</u>", escaped)


def explain_in_label(label: QLabel, text: str) -> None:
    """Shows `text` in `label`, the word underlined and explained on hover."""
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(underlined_html(text))
    label.setToolTip(explanation() if mentions_retrievability(text) else "")

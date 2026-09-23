# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import pytest

import anki.lang


@pytest.fixture(autouse=True, scope="session")
def _translation_backend() -> None:
    """Every test file can call `tr` when it runs alone.

    `tr` needs a translation backend. The full suite only had one because an
    earlier test file set a language; `test_rwkv_scheduler.py` on its own then
    failed 62 tests with "'NoneType' object is not callable" from
    `Translations._translate`. A test file that sets a language keeps it."""
    if anki.lang.current_i18n is None:
        anki.lang.set_lang("en")

# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The app's log also goes to a file in the logs folder, with the scheduling
passes at debug level, while stdout keeps the app's level."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import aqt.log


@pytest.fixture
def restore_logging() -> Iterator[None]:
    manager = logging.Logger.manager
    root_handlers = logging.root.handlers[:]
    root_level = logging.root.level
    levels = {name: logging.getLogger(name).level for name in aqt.log.APP_DEBUG_LOGGERS}
    yield
    for handler in logging.root.handlers:
        handler.close()
    logging.Logger.manager = manager
    logging.root.handlers[:] = root_handlers
    logging.root.setLevel(root_level)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    logging.captureWarnings(False)


def test_the_log_file_holds_info_lines_and_scheduling_debug_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    aqt.log.setup_logging(tmp_path, level=logging.INFO)

    logging.getLogger("aqt.main").info("an info line")
    logging.getLogger("aqt.main").debug("a debug line of another module")
    logging.getLogger("aqt.rwkv_scheduler").debug("an rwkv debug line")
    logging.getLogger("aqt.fsrs_predictions").debug("an fsrs debug line")
    for handler in logging.root.handlers:
        handler.flush()

    text = (tmp_path / aqt.log.APP_LOG_FILE).read_text(encoding="utf-8")
    assert "an info line" in text
    assert "an rwkv debug line" in text
    assert "an fsrs debug line" in text
    assert "a debug line of another module" not in text

    out = capsys.readouterr().out
    assert "an info line" in out
    assert "an rwkv debug line" not in out
    assert "an fsrs debug line" not in out


def test_the_log_file_is_kept_to_a_few_rotated_files(
    tmp_path: Path, restore_logging: None
) -> None:
    aqt.log.setup_logging(tmp_path, level=logging.INFO)

    handlers = [
        h
        for h in logging.root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == aqt.log.APP_LOG_MAX_BYTES
    assert handlers[0].backupCount == aqt.log.APP_LOG_BACKUPS

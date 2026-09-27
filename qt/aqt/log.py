# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, cast

# The app's own log file in the logs folder, kept to 5 files of 10 MB
APP_LOG_FILE = "clanki.log"
APP_LOG_MAX_BYTES = 10 * 1024 * 1024
APP_LOG_BACKUPS = 4
# Loggers whose debug lines go to the log file (stdout keeps the app's level)
APP_DEBUG_LOGGERS = ("aqt.rwkv_scheduler", "aqt.fsrs_predictions")

# All loggers with the following prefix will be treated as add-on loggers
#
# To instatiate a logger with this prefix, use aqt.AddonManager.get_logger()
#
# NOTE: Add-ons might also directly instantiate a logger with this prefix, e.g. in
#       order to avoid depending on the Anki codebase, so this prefix should not
#       be changed.
ADDON_LOGGER_PREFIX = "addon."

# Formatter used for all loggers
FORMATTER = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")


class AnkiLoggerManager(logging.Manager):
    # inspired by: https://github.com/abdnh/ankiutils/blob/master/src/ankiutils/log.py

    def __init__(
        self,
        logs_path: Path | str,
        existing_loggers: dict[str, logging.Logger | logging.PlaceHolder],
        rootnode: logging.RootLogger,
    ):
        super().__init__(rootnode)
        self.loggerDict = existing_loggers
        self.logs_path = Path(logs_path)

    def getLogger(self, name: str) -> logging.Logger:
        if not name.startswith(ADDON_LOGGER_PREFIX) or name in self.loggerDict:
            return super().getLogger(name)

        module = name.removeprefix(ADDON_LOGGER_PREFIX).split(".", maxsplit=1)[0]
        addon_logger_name = f"{ADDON_LOGGER_PREFIX}{module}"

        if addon_logger_name not in self.loggerDict:
            # Create a new add-on logger
            logger = super().getLogger(addon_logger_name)

            path = (
                get_addon_logs_folder(self.logs_path, module=module) / f"{module}.log"
            )
            path.parent.mkdir(parents=True, exist_ok=True)

            # Keep the last 10 days of logs
            handler = TimedRotatingFileHandler(
                filename=path, when="D", interval=1, backupCount=10, encoding="utf-8"
            )
            handler.setFormatter(FORMATTER)

            logger.addHandler(handler)

        return super().getLogger(name)


def get_addon_logs_folder(logs_path: Path | str, module: str) -> Path:
    return Path(logs_path) / "addons" / module


def find_addon_logger(module: str) -> logging.Logger | None:
    return cast(
        Optional[logging.Logger],
        logging.Logger.manager.loggerDict.get(f"{ADDON_LOGGER_PREFIX}{module}"),
    )


def setup_logging(path: Path | str, **kwargs) -> None:
    """
    Set up logging for the application.

    Configures the root logger to output logs to stdout by default, with custom
    handling for add-on logs. The add-on logs are saved to a separate folder and file
    for each add-on, under the path provided.

    Args:
        path (Path): The path where the log files should be stored.
        **kwargs: Arbitrary keyword arguments for logging.basicConfig
    """

    # Patch root logger manager to handle add-on loggers
    logger_manager = AnkiLoggerManager(
        path, existing_loggers=logging.Logger.manager.loggerDict, rootnode=logging.root
    )
    logging.Logger.manager = logger_manager

    level = kwargs.get("level", logging.WARNING)
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(FORMATTER)
    stdout_handler.setLevel(level)
    # pythonw has no stdout, so the app's own log also goes to a file in the
    # logs folder, with the scheduling passes at debug level: without it a
    # wait in the reviewer leaves nothing to read afterwards
    file_handler = RotatingFileHandler(
        Path(path) / APP_LOG_FILE,
        maxBytes=APP_LOG_MAX_BYTES,
        backupCount=APP_LOG_BACKUPS,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(FORMATTER)
    file_handler.setLevel(logging.DEBUG)
    logging.basicConfig(handlers=[stdout_handler, file_handler], force=True, **kwargs)
    for name in APP_DEBUG_LOGGERS:
        if logging.getLogger(name).getEffectiveLevel() > logging.DEBUG:
            logging.getLogger(name).setLevel(logging.DEBUG)
    logging.captureWarnings(True)

    # Silence some loggers of external libraries:
    silenced_loggers = [
        "waitress.queue",
    ]
    for logger in silenced_loggers:
        logging.getLogger(logger).setLevel(logging.CRITICAL)
        logging.getLogger(logger).propagate = False

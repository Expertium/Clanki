# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest

import aqt.errors
import aqt.main
import aqt.rwkv_scheduler
import aqt.sync
from anki.collection import OpChanges
from aqt.main import AnkiQt


class CloseEvent:
    def __init__(self) -> None:
        self.ignored = False
        self.accepted = False

    def ignore(self) -> None:
        self.ignored = True

    def accept(self) -> None:
        self.accepted = True


class Progress:
    def __init__(self) -> None:
        self.scheduled: list[Callable[[], None]] = []
        self.started: list[dict[str, object]] = []
        self.finished = 0
        self.cancel_wanted = False

    def single_shot(
        self,
        ms: int,
        func: Callable[[], None],
        requires_collection: bool = True,
        *,
        even_with_progress: bool = False,
    ) -> None:
        # the real manager swallows a callback while a progress window is up,
        # so a loop that opened its own window must say so or never run again
        if self.started and self.finished == 0 and not even_with_progress:
            return
        self.scheduled.append(func)

    def start(self, **kwargs: object) -> None:
        self.started.append(kwargs)

    def finish(self) -> None:
        self.finished += 1

    def want_cancel(self) -> bool:
        return self.cancel_wanted


def setup_mw() -> tuple[AnkiQt, list[str], Progress]:
    mw = AnkiQt.__new__(AnkiQt)
    progress = Progress()
    calls: list[str] = []

    mw.state = "deckBrowser"
    mw.progress = progress
    mw._background_op_count = 0
    mw._unload_profile_and_exit_pending = False
    mw._collection_closing = False
    mw._close_wait_started = 0.0
    mw._close_wait_window_shown = False
    mw.unloadProfileAndExit = lambda: calls.append("unload")  # type: ignore[method-assign]

    return mw, calls, progress


def test_study_queue_mutation_invalidates_rwkv_before_screen_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.reviewer = object()
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "study_queues_did_change",
        lambda _owner, _initiator, op_changes: calls.append(
            "rwkv" if op_changes is changes else "wrong changes"
        ),
    )
    changes = OpChanges()
    changes.study_queues = True

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["rwkv", "screen"]


def test_non_queue_preset_mutation_invalidates_rwkv_before_screen_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "fsrs_preset_resolution_did_change",
        lambda _owner: calls.append("rwkv preset"),
    )
    changes = OpChanges()
    changes.deck_config = True

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["rwkv preset", "screen"]


def test_note_content_mutation_preserves_unchanged_rwkv_state_before_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    initiator = object()
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "collection_content_did_change",
        lambda _owner, handler: calls.append(
            "rwkv content" if handler is initiator else "wrong initiator"
        ),
    )
    changes = OpChanges()
    changes.note = True
    changes.note_text = True

    mw.on_operation_did_execute(changes, handler=initiator)

    assert calls == ["rwkv content", "screen"]


def test_startup_sync_can_defer_rwkv_refresh(
    monkeypatch,
) -> None:
    calls: list[object] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.can_auto_sync = lambda: True  # type: ignore[method-assign]

    def sync(
        after_sync: Callable[[], None],
        *,
        refresh_rwkv_state: bool = True,
    ) -> None:
        calls.append(("sync", refresh_rwkv_state))
        after_sync()

    mw._sync_collection_and_media = sync  # type: ignore[method-assign]

    mw.maybe_auto_sync_on_open_close(
        lambda synced: calls.append(("done", synced)),
        refresh_rwkv_state=False,
    )

    assert calls == [
        ("sync", False),
        ("done", True),
    ]


def test_sync_can_finish_without_separate_rwkv_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(
            aqt.sync.RemoteCollectionChanges(collection_changed=True)
        )
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        lambda _mw, _on_done: calls.append("rwkv refresh"),
    )

    mw._sync_collection_and_media(
        lambda: calls.append("done"),
        refresh_rwkv_state=False,
    )

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "done",
    ]


def test_sync_skips_rwkv_refresh_without_remote_collection_changes(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(aqt.sync.RemoteCollectionChanges())
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        lambda _mw, _on_done: calls.append("rwkv refresh"),
    )

    mw._sync_collection_and_media(lambda: calls.append("done"))

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "done",
    ]


def test_sync_resets_ui_before_refreshing_rwkv_for_remote_collection_changes(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(
            aqt.sync.RemoteCollectionChanges(
                collection_changed=True,
                review_ids=(123,),
            )
        )
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)

    def refresh(
        _mw: object,
        on_done: Callable[[], None],
        *,
        remote_review_ids: tuple[int, ...],
    ) -> None:
        calls.append(f"rwkv refresh {remote_review_ids}")
        on_done()

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        refresh,
    )

    mw._sync_collection_and_media(lambda: calls.append("done"))

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "rwkv refresh (123,)",
        "done",
    ]


def test_profile_load_marks_rwkv_startup_before_loading_collection(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.loadCollection = lambda: calls.append("load collection") or False  # type: ignore[method-assign]
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "begin_rwkv_state_cache_startup",
        lambda _mw: calls.append("begin rwkv"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "finish_rwkv_state_cache_startup",
        lambda _mw: calls.append("finish rwkv"),
    )

    mw.loadProfile()

    assert calls == [
        "begin rwkv",
        "load collection",
        "finish rwkv",
    ]


def test_profile_unload_cancels_rwkv_counts_before_closing_collection(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.deckBrowser = SimpleNamespace(
        cancel_rwkv_count_refresh=lambda: calls.append("cancel rwkv counts")
    )
    mw.unloadCollection = lambda _callback: calls.append(  # type: ignore[method-assign]
        "unload collection"
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "profile_will_close",
        lambda: calls.append("profile will close"),
    )

    mw.unloadProfile(lambda: calls.append("done"))

    assert calls == [
        "cancel rwkv counts",
        "profile will close",
        "unload collection",
    ]


def test_close_event_unloads_profile_when_no_background_op() -> None:
    mw, calls, progress = setup_mw()
    event = CloseEvent()

    mw.closeEvent(event)  # type: ignore[arg-type]

    assert event.ignored
    assert calls == ["unload"]
    assert not progress.scheduled


def test_close_event_waits_for_background_op_before_unloading_profile() -> None:
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    event = CloseEvent()

    mw.closeEvent(event)  # type: ignore[arg-type]
    mw.closeEvent(event)  # type: ignore[arg-type]

    assert event.ignored
    assert calls == []
    assert len(progress.scheduled) == 1

    mw._background_op_count = 0
    progress.scheduled.pop()()

    assert calls == ["unload"]


def test_cleanup_and_exit_closes_profile_manager(monkeypatch) -> None:
    mw = AnkiQt.__new__(AnkiQt)
    progress = Progress()
    calls: list[str] = []

    mw.errorHandler = type(
        "ErrorHandler", (), {"unload": lambda self: calls.append("unload_errors")}
    )()
    mw.mediaServer = type(
        "MediaServer", (), {"shutdown": lambda self: calls.append("shutdown_media")}
    )()
    mw.backend = type(
        "Backend",
        (),
        {
            "await_backup_completion": lambda self: calls.append(
                "await_backup_completion"
            )
        },
    )()
    mw.pm = type(
        "ProfileManager", (), {"close": lambda self: calls.append("close_pm")}
    )()
    mw.toolbarWeb = type(
        "ToolbarWeb", (), {"cleanup": lambda self: calls.append("cleanup_toolbar")}
    )()
    mw.web = type("Web", (), {"cleanup": lambda self: calls.append("cleanup_web")})()
    mw.bottomWeb = type(
        "BottomWeb", (), {"cleanup": lambda self: calls.append("cleanup_bottom")}
    )()
    mw.app = type(
        "App",
        (),
        {
            "_unset_windows_shutdown_block_reason": lambda self: calls.append(
                "unset_shutdown_block"
            ),
            "exit": lambda self, code: calls.append(f"exit:{code}"),
        },
    )()
    mw.progress = progress
    mw.deleteLater = lambda: calls.append("delete_later")  # type: ignore[method-assign]
    monkeypatch.setattr(aqt.main.gc, "collect", lambda: calls.append("gc"))

    mw.cleanupAndExit()

    assert calls == [
        "unload_errors",
        "shutdown_media",
        "await_backup_completion",
        "close_pm",
        "cleanup_toolbar",
        "cleanup_web",
        "cleanup_bottom",
        "delete_later",
        "unset_shutdown_block",
    ]
    assert len(progress.scheduled) == 1

    progress.scheduled.pop()()

    assert calls[-2:] == ["gc", "exit:0"]


def test_error_handler_unload_keeps_excepthook_and_detaches_logging_stream(
    monkeypatch,
) -> None:
    old_stderr = sys.stderr
    previous_excepthook = sys.excepthook

    def excepthook(etype: object, value: object, tb: object) -> None:
        pass

    logger = logging.getLogger("test_error_handler_unload")
    logger.handlers.clear()

    handler = type("ErrorHandler", (), {})()
    handler._oldstderr = old_stderr
    stream_handler = logging.StreamHandler(stream=handler)
    logger.addHandler(stream_handler)

    monkeypatch.setattr(sys, "stderr", handler)
    monkeypatch.setattr(sys, "excepthook", excepthook)

    try:
        aqt.errors.ErrorHandler.unload(handler)

        assert sys.stderr is old_stderr
        assert sys.excepthook is excepthook
        assert stream_handler.stream is old_stderr
    finally:
        logger.handlers.clear()
        sys.excepthook = previous_excepthook


@pytest.mark.parametrize(
    ("state", "busy", "backs_up"),
    [
        ("deckBrowser", False, True),
        ("overview", False, True),
        ("review", False, False),
        ("deckBrowser", True, False),
    ],
)
def test_periodic_backup_waits_for_reviews_and_other_collection_work(
    state: str, busy: bool, backs_up: bool
) -> None:
    mw = AnkiQt.__new__(AnkiQt)
    calls: list[bool] = []
    mw.state = state  # type: ignore[assignment]
    mw.taskman = SimpleNamespace(collection_busy=lambda: busy)  # type: ignore[assignment]

    def create_backup_with_progress(user_initiated: bool) -> None:
        calls.append(user_initiated)

    mw._create_backup_with_progress = create_backup_with_progress  # type: ignore[method-assign]

    AnkiQt.on_periodic_backup_timer(mw)

    assert calls == ([False] if backs_up else [])


def test_collection_busy_counts_queued_and_running_collection_tasks() -> None:
    from aqt.taskman import TaskManager

    mw = SimpleNamespace()
    mw.weakref = lambda: mw
    taskman = TaskManager(mw)  # type: ignore[arg-type]
    release = threading.Event()

    other = taskman.run_in_background(release.wait, uses_collection=False)
    assert not taskman.collection_busy()
    running = taskman.run_in_background(release.wait)
    queued = taskman.run_in_background(lambda: None)
    assert taskman.collection_busy()

    release.set()
    for future in (other, running, queued):
        future.result(timeout=10)
    # the count drops in the future's done callback, just after its result
    deadline = time.monotonic() + 10
    while taskman.collection_busy() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not taskman.collection_busy()

    taskman.collection_use_started()
    assert taskman.collection_busy()
    taskman.collection_use_finished()
    assert not taskman.collection_busy()


def test_start_up_objects_are_frozen_but_later_cycles_are_still_collected() -> None:
    """The window's manual collections must not walk what start-up created."""
    import gc

    mw = AnkiQt.__new__(AnkiQt)
    frozen_before = gc.get_freeze_count()
    try:
        mw.freeze_startup_objects()
        assert gc.get_freeze_count() > frozen_before
        held: dict = {}
        cycle = [held]
        held["cycle"] = cycle
        del held, cycle
        assert gc.collect() >= 2
    finally:
        gc.unfreeze()


def test_start_up_garbage_is_not_frozen_with_the_rest() -> None:
    """Start-up runs with automatic collection off, so it leaves cycles
    behind; freezing them would keep them for the rest of the session."""
    import gc
    import weakref

    class Node:
        def __init__(self) -> None:
            self.other: Node | None = None

    node = Node()
    node.other = node
    watcher = weakref.ref(node)
    del node
    mw = AnkiQt.__new__(AnkiQt)
    frozen_before = gc.get_freeze_count()
    try:
        mw.freeze_startup_objects()
        assert gc.get_freeze_count() > frozen_before
        assert watcher() is None
    finally:
        gc.unfreeze()


# Pins spec/ui.md#ui.background-change-dim: a screen dimmed by a background
# change returns to full opacity when the window gets focus again.
def test_focus_undims_the_deck_list_and_the_overview() -> None:
    from types import SimpleNamespace

    for state in ("deckBrowser", "overview"):
        mw = AnkiQt.__new__(AnkiQt)
        mw.state = state
        refreshed: list[str] = []
        faded: list[str] = []
        screen = SimpleNamespace(
            refresh_if_needed=lambda state=state: refreshed.append(state)
        )
        mw.deckBrowser = screen
        mw.overview = screen
        mw.fade_in_webview = lambda: faded.append("in")  # type: ignore[method-assign]

        window = SimpleNamespace(window=lambda: mw)
        AnkiQt.on_focus_did_change(mw, window, None)  # type: ignore[arg-type]

        assert refreshed == [state]
        assert faded == ["in"]


# Pins spec/ui.md#ui.close-says-what-it-waits-for


def test_a_close_with_nothing_running_opens_no_window() -> None:
    """The usual close is instant, so it still shows nothing
    (spec ui.no-waiting-windows)."""
    mw, calls, progress = setup_mw()

    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]

    assert calls == ["unload"]
    assert progress.started == []


def test_a_close_that_waits_says_what_it_waits_for(monkeypatch) -> None:
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    clock = [1000.0]
    monkeypatch.setattr(aqt.main.time, "monotonic", lambda: clock[0])

    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]
    # inside the grace period: still silent
    assert progress.started == []

    clock[0] += AnkiQt.CLOSE_WAIT_GRACE_SECS + 0.1
    progress.scheduled.pop()()

    assert len(progress.started) == 1
    started = progress.started[0]
    assert "Clanki" in str(started["title"])
    assert "background work" in str(started["label"]).lower()
    # the wait can be stopped, and the window says so
    assert started["cancel_label"]
    assert calls == []

    # the window is opened once, not on every retry
    clock[0] += 5
    progress.scheduled.pop()()
    assert len(progress.started) == 1


def test_the_close_wait_window_closes_when_the_work_finishes(monkeypatch) -> None:
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    clock = [1000.0]
    monkeypatch.setattr(aqt.main.time, "monotonic", lambda: clock[0])

    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]
    clock[0] += AnkiQt.CLOSE_WAIT_GRACE_SECS + 0.1
    progress.scheduled.pop()()
    assert len(progress.started) == 1

    mw._background_op_count = 0
    progress.scheduled.pop()()

    assert progress.finished == 1
    assert calls == ["unload"]


def test_cancelling_the_close_wait_keeps_the_app_open(monkeypatch) -> None:
    """Cancel abandons the close. Closing regardless is what the wait exists
    to prevent (docs/collection-shutdown.MD)."""
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    clock = [1000.0]
    monkeypatch.setattr(aqt.main.time, "monotonic", lambda: clock[0])

    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]
    clock[0] += AnkiQt.CLOSE_WAIT_GRACE_SECS + 0.1
    progress.scheduled.pop()()

    progress.cancel_wanted = True
    progress.scheduled.pop()()

    assert progress.finished == 1
    assert calls == []
    assert progress.scheduled == []
    # the app stays open, and closing again starts a new wait
    assert not mw._unload_profile_and_exit_pending

    progress.cancel_wanted = False
    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]
    assert len(progress.scheduled) == 1


def test_the_close_wait_keeps_polling_while_its_own_window_is_open(
    monkeypatch,
) -> None:
    """The window this loop opens must not stop the loop.

    ProgressManager.single_shot swallows a callback while a progress window
    is up. The close-wait loop opens that window itself, so without
    even_with_progress it would wait for itself and never close
    (spec ui.close-says-what-it-waits-for).
    """
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    clock = [1000.0]
    monkeypatch.setattr(aqt.main.time, "monotonic", lambda: clock[0])
    asked: list[bool] = []
    real_single_shot = progress.single_shot

    def recording_single_shot(
        ms: int,
        func: Callable[[], None],
        requires_collection: bool = True,
        *,
        even_with_progress: bool = False,
    ) -> None:
        asked.append(even_with_progress)
        real_single_shot(
            ms, func, requires_collection, even_with_progress=even_with_progress
        )

    progress.single_shot = recording_single_shot  # type: ignore[method-assign]

    mw.closeEvent(CloseEvent())  # type: ignore[arg-type]
    clock[0] += AnkiQt.CLOSE_WAIT_GRACE_SECS + 0.1
    progress.scheduled.pop()()
    assert len(progress.started) == 1

    # the retry after the window opened asked to run anyway, and was scheduled
    assert asked[-1] is True
    assert len(progress.scheduled) == 1

    mw._background_op_count = 0
    progress.scheduled.pop()()
    assert calls == ["unload"]


# Pins spec/ui.md#ui.close-off-main-thread


class _ClosingCollection:
    """Records what the close does with the collection, and on which thread."""

    def __init__(self, quick_check: str = "ok") -> None:
        self.calls: list[str] = []
        self.threads: set[str] = set()
        self._quick_check = quick_check
        self.db = SimpleNamespace(scalar=self._scalar)
        self._backend = SimpleNamespace(
            await_backup_completion=lambda: self._call("await backup")
        )

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.threads.add(threading.current_thread().name)

    def _scalar(self, sql: str) -> str:
        self._call(sql)
        return self._quick_check

    def optimize(self) -> None:
        self._call("optimize")

    def create_backup(self, **kwargs: object) -> None:
        self._call("backup")

    def close(self, downgrade: bool) -> None:
        self._call("close")


class _Taskman:
    def __init__(self) -> None:
        self.tasks: list[tuple[Callable[[], object], Callable]] = []

    def run_in_background(self, task: Callable[[], object], on_done: Callable) -> None:
        self.tasks.append((task, on_done))

    def run(self) -> None:
        """Run the queued task on another thread, then its on_done here."""
        from concurrent.futures import ThreadPoolExecutor

        task, on_done = self.tasks.pop()
        with ThreadPoolExecutor(thread_name_prefix="close") as pool:
            future = pool.submit(task)
            future.result()
        on_done(future)


def _closing_mw(
    col: _ClosingCollection, optimize_due: bool = False
) -> tuple[AnkiQt, Progress, _Taskman, list[str]]:
    mw, _calls, progress = setup_mw()
    taskman = _Taskman()
    done: list[str] = []
    mw.col = col  # type: ignore[assignment]
    mw.taskman = taskman  # type: ignore[assignment]
    mw.restoring_backup = False
    last = 0 if optimize_due else aqt.main.int_time()
    mw.pm = SimpleNamespace(  # type: ignore[assignment]
        backupFolder=lambda: "backups",
        profile={"lastOptimize": last},
        save=lambda: done.append("pm saved"),
    )
    return mw, progress, taskman, done


def test_the_collection_closes_on_a_background_thread(monkeypatch) -> None:
    monkeypatch.setattr(aqt.main, "dev_mode", False)
    col = _ClosingCollection()
    mw, progress, taskman, done = _closing_mw(col)

    mw._unloadCollection(lambda: done.append("unloaded"))

    # nothing has touched the collection yet, and the main thread can no
    # longer reach it
    assert col.calls == []
    assert mw.col is None
    assert done == []
    # a second close request while it closes is ignored
    event = CloseEvent()
    mw.closeEvent(event)  # type: ignore[arg-type]
    assert event.ignored and mw._unload_profile_and_exit_pending is False

    taskman.run()

    # only the collection is checked, not the attached cache (B-027)
    assert col.calls == ["pragma main.quick_check", "backup", "close", "await backup"]
    assert threading.current_thread().name not in col.threads
    assert done == ["unloaded"]
    assert mw._collection_closing is False
    # a close within the grace time shows no window
    assert progress.started == []


def test_a_slow_close_says_it_is_backing_up(monkeypatch) -> None:
    monkeypatch.setattr(aqt.main, "dev_mode", False)
    col = _ClosingCollection()
    mw, progress, taskman, done = _closing_mw(col)

    mw._unloadCollection(lambda: done.append("unloaded"))
    # the grace time passes before the close ends
    progress.scheduled.pop()()

    from aqt.utils import tr

    labels = [started["label"] for started in progress.started]
    assert labels == [tr.qt_misc_backing_up()]
    taskman.run()
    assert progress.finished == 1
    assert done == ["unloaded"]


def test_a_due_optimize_runs_with_the_close(monkeypatch) -> None:
    monkeypatch.setattr(aqt.main, "dev_mode", False)
    col = _ClosingCollection()
    mw, progress, taskman, done = _closing_mw(col, optimize_due=True)

    mw._unloadCollection(lambda: done.append("unloaded"))
    taskman.run()

    assert col.calls[0] == "optimize"
    assert mw.pm.profile["lastOptimize"] > 0
    assert done == ["pm saved", "unloaded"]


def test_a_corrupt_collection_is_not_backed_up_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(aqt.main, "dev_mode", False)
    warnings: list[str] = []
    monkeypatch.setattr(aqt.main, "showWarning", warnings.append)
    col = _ClosingCollection(quick_check="*** in database main ***")
    mw, progress, taskman, done = _closing_mw(col)

    mw._unloadCollection(lambda: done.append("unloaded"))
    taskman.run()

    assert "backup" not in col.calls
    assert "close" in col.calls
    assert len(warnings) == 1
    assert done == ["unloaded"]


def _rollover_mw(
    monkeypatch, state: str, cutoff: int
) -> tuple[AnkiQt, list[str], list[int]]:
    """A main window at the day-rollover check: `calls` records the
    reviewer refreshes and the day_did_change calls, `timers` the delay of
    each next check in ms."""
    calls: list[str] = []
    timers: list[int] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = state
    mw.col = SimpleNamespace(sched=SimpleNamespace(day_cutoff=cutoff))  # type: ignore[assignment]

    def refresh_if_needed() -> None:
        calls.append(f"reviewer refresh {mw.reviewer._refresh_needed.name}")

    mw.reviewer = SimpleNamespace(  # type: ignore[assignment]
        _refresh_needed=None, refresh_if_needed=refresh_if_needed
    )
    mw.progress = SimpleNamespace(  # type: ignore[assignment]
        timer=lambda ms, func, repeat, parent: timers.append(ms)
    )
    monkeypatch.setattr(aqt.main, "int_time", lambda: 1_000)
    monkeypatch.setattr(
        aqt.main.gui_hooks, "day_did_change", lambda: calls.append("day_did_change")
    )
    mw._last_day_cutoff = cutoff
    return mw, calls, timers


# Pins spec/ui.md#ui.day-rollover
def test_the_day_rollover_fires_day_did_change_in_the_reviewer(monkeypatch) -> None:
    """The check updated the remembered cutoff while reviewing, and then
    compared the updated value to decide on the hook, so the hook never
    fired while the reviewer was open."""

    mw, calls, timers = _rollover_mw(monkeypatch, "review", cutoff=900)
    mw.col.sched.day_cutoff = 900 + 86_400

    mw._check_day_rollover()

    assert calls == ["reviewer refresh QUEUES", "day_did_change"]
    # and the next check waits for the next cutoff
    assert timers == [(900 + 86_400 - 1_000) * 1000]


# Pins spec/ui.md#ui.day-rollover
def test_the_day_rollover_fires_day_did_change_once_outside_the_reviewer(
    monkeypatch,
) -> None:
    mw, calls, timers = _rollover_mw(monkeypatch, "deckBrowser", cutoff=900)
    mw.col.sched.day_cutoff = 900 + 86_400

    mw._check_day_rollover()
    # a check that comes again on the same day (a timer that fires early)
    # is no second rollover
    mw._check_day_rollover()

    assert calls == ["day_did_change"]
    assert len(timers) == 2


# Pins spec/ui.md#ui.day-rollover
def test_no_rollover_changes_nothing(monkeypatch) -> None:
    mw, calls, timers = _rollover_mw(monkeypatch, "review", cutoff=5_000)

    mw._check_day_rollover()

    assert calls == []
    assert timers == [4_000_000]

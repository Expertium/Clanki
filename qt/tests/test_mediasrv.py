# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for mediasrv security utilities."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from aqt.mediasrv import (
    TRUSTED_PAGE_CSP,
    UNTRUSTED_MEDIA_CSP,
    BundledFileRequest,
    LegacyPage,
    LocalFileRequest,
    PageContext,
    UnsafePathException,
    _editor_content_security_policy,
    _handle_builtin_file_request,
    _handle_local_file_request,
    _legacy_editor_content_security_policy,
    _rwkv_raw_backend_mutation_note_ids,
    _should_log_request,
    post_handler_list,
    _untrusted_sveltekit_content_security_policy,
    ensure_safe_path,
    get_sveltekit_route,
    is_localhost_origin,
    legacy_page_data,
)


def test_rwkv_raw_backend_mutation_scopes() -> None:
    from anki import image_occlusion_pb2, notes_pb2

    update_notes = notes_pb2.UpdateNotesRequest(
        notes=[notes_pb2.Note(id=10), notes_pb2.Note(id=20)]
    )
    update_image = image_occlusion_pb2.UpdateImageOcclusionNoteRequest(note_id=30)

    assert _rwkv_raw_backend_mutation_note_ids("add_note", b"") == ()
    assert _rwkv_raw_backend_mutation_note_ids("add_image_occlusion_note", b"") == ()
    assert _rwkv_raw_backend_mutation_note_ids(
        "update_notes",
        update_notes.SerializeToString(),
    ) == (10, 20)
    assert _rwkv_raw_backend_mutation_note_ids(
        "update_image_occlusion_note",
        update_image.SerializeToString(),
    ) == (30,)
    assert _rwkv_raw_backend_mutation_note_ids("remove_notes", b"") is None


def test_post_handler_list_has_no_rwkv_workload_handlers() -> None:
    # Pins spec/deck-options.md deck-options.simulator-fsrs-only: the desktop
    # no longer serves the RWKV workload simulation endpoints.
    from aqt import rwkv_scheduler

    handler_names = {handler.__name__ for handler in post_handler_list}

    assert "reschedule_rwkv_review_cards" in handler_names
    assert not {name for name in handler_names if "workload" in name}
    assert not hasattr(rwkv_scheduler, "simulate_rwkv_workload")


class TestEnsureSafePath:
    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        subdir = Path(self.tmpdir) / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("ok")

    def test_valid_subpath(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub/file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "../etc/passwd")

    def test_rejects_double_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../etc/passwd")

    def test_rejects_absolute_path_escape(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "/etc/passwd")

    def test_rejects_base_dir_itself(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, ".")

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "")

    def test_accepts_pathlib_args(self) -> None:
        result = ensure_safe_path(Path(self.tmpdir), Path("sub/file.txt"))
        assert result.endswith(os.path.join("sub", "file.txt"))

    def test_normalizes_redundant_separators(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub///file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_traversal_after_normalization(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../../etc/passwd")


class TestIsLocalhostOrigin:
    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:40000",
            "http://localhost:40000",
            "http://[::1]:40000",
            "https://127.0.0.1:40000",
            "https://localhost:40000",
            "https://[::1]:40000",
            "http://127.0.0.1",
            "http://localhost",
            "http://[::1]",
            "http://127.0.0.1/",
            "http://localhost/path",
        ],
    )
    def test_allowed_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.com",
            "http://127.0.0.1.evil.com",
            "http://localhost.evil.com",
            "http://evil.com:127.0.0.1",
            "http://notlocalhost:40000",
            "https://evil.com",
            "",
        ],
    )
    def test_rejected_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is False


class TestGetSveltekitRoute:
    def test_deck_options_is_internal_page(self) -> None:
        assert get_sveltekit_route("deck-options") == "deck-options"
        assert get_sveltekit_route("deck-options/_app/start.js") == "deck-options"

    def test_removed_dynamic_desired_retention_plot_is_not_a_page(self) -> None:
        assert get_sveltekit_route("dynamic-desired-retention-plot") is None


class TestRequestLogging:
    def test_latest_progress_polling_is_not_logged(self) -> None:
        assert not _should_log_request("_anki/latestProgress")

    def test_regular_requests_are_logged(self) -> None:
        assert _should_log_request("_anki/evaluateParamsLegacy")


class TestGraphs:
    @pytest.mark.parametrize(
        ("status", "expected_header"),
        [
            ("PENDING", "1"),
            ("READY", None),
        ],
    )
    def test_graphs_sets_rwkv_pending_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        expected_header: str | None,
    ) -> None:
        import aqt
        from anki.stats_pb2 import GraphsRequest
        from aqt.mediasrv import RWKV_STATS_PENDING_HEADER, app, graphs
        from aqt.rwkv_scheduler import RwkvStatsPreparationStatus

        calls: list[str] = []

        def prepare(reviewer: object, search: str) -> RwkvStatsPreparationStatus:
            calls.append(search)
            return getattr(RwkvStatsPreparationStatus, status)

        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=object()), raising=False)
        monkeypatch.setattr(
            "aqt.rwkv_scheduler.prepare_stats_retrievability_scores",
            prepare,
        )
        monkeypatch.setattr(
            "aqt.mediasrv.raw_backend_request",
            lambda endpoint: lambda: b"graph-data",
        )

        data = GraphsRequest(search="rated:7", days=365).SerializeToString()
        with app.test_request_context(data=data):
            response = graphs()

        assert calls == ["rated:7"]
        assert response.get_data() == b"graph-data"
        assert response.headers.get("Content-Type") == "application/binary"
        assert response.headers.get(RWKV_STATS_PENDING_HEADER) == expected_header


def _make_media_file(tmpdir: str, filename: str, content: bytes = b"test") -> str:
    path = os.path.join(tmpdir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return filename


def _get_csp(response) -> str | None:
    return response.headers.get("Content-Security-Policy")


def _csp_directives(csp: str) -> dict[str, str]:
    directives = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        directives[name] = value
    return directives


class TestMediaFileCSP:
    """CSP headers on media file responses should block script execution."""

    @pytest.mark.parametrize("doctype", ["html", "svg"])
    def test_doc_has_csp_header(self, doctype: str) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(
                tmpdir, f"test.{doctype}", f"<{doctype}></{doctype}>".encode()
            )
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp is not None, f"{doctype} response must have CSP header"

    def test_csp_blocks_connect_to_local_api(self) -> None:
        """Scripts must not be able to fetch() the local /_anki/ API.

        Even if script-src somehow gets relaxed in the future, connect-src
        should not allow http: (which includes http://127.0.0.1).
        """
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "test.svg", b"<svg></svg>")
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp is not None

            directives = _csp_directives(csp)
            connect_src = directives.get("connect-src", directives.get("default-src"))
            assert connect_src == "'none'", (
                f"CSP must not allow connections (enables local API access): {csp}"
            )

    def test_untrusted_media_is_sandboxed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "test.svg", b"<svg></svg>")
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp == UNTRUSTED_MEDIA_CSP

            directives = _csp_directives(csp)
            assert directives["default-src"] == "'none'"
            assert directives["script-src"] == "'none'"
            assert directives["connect-src"] == "'none'"
            assert directives["object-src"] == "'none'"
            assert directives["frame-src"] == "'none'"
            assert directives["child-src"] == "'none'"
            assert directives["base-uri"] == "'none'"
            assert directives["form-action"] == "'none'"
            assert directives["sandbox"] == "allow-same-origin"
            assert "frame-ancestors" not in directives

    def test_untrusted_media_can_load_its_own_presentation(self) -> None:
        """Media embedded via <object>/<iframe> is a document of its own, and has to
        be able to load the stylesheets/images/fonts stored next to it."""
        directives = _csp_directives(UNTRUSTED_MEDIA_CSP)

        assert directives["style-src"] == "'self' 'unsafe-inline'"
        assert directives["img-src"] == "'self'"
        assert directives["font-src"] == "'self'"
        assert directives["media-src"] == "'self'"

        # ...but only passive resources, and only from our own server, so that
        # cards can neither execute code nor phone home
        for directive in ("script-src", "connect-src", "object-src", "frame-src"):
            assert directives[directive] == "'none'"
        for name, value in directives.items():
            assert not any(
                remote in value for remote in ("http:", "https:", "data:", "*")
            ), f"{name} must not allow remote sources: {value}"

    def test_trusted_local_file_does_not_get_untrusted_media_csp(self) -> None:
        """Add-on exports use LocalFileRequest too, but should not be sandboxed."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "addon.html", b"<html></html>")
            req = LocalFileRequest(root=tmpdir, path=fname, untrusted=False)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            assert _get_csp(resp) is None


class TestRwkvReschedule:
    @pytest.mark.parametrize("deck_id", [None, 100])
    def test_reschedule_forwards_requested_deck_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deck_id: int | None,
    ) -> None:
        import aqt
        from anki import decks_pb2
        from aqt.mediasrv import app, reschedule_rwkv_review_cards

        mw = object()
        calls: list[tuple[object, int | None]] = []

        def reschedule(mw_arg: object, *, deck_id: int | None = None) -> None:
            calls.append((mw_arg, deck_id))

        monkeypatch.setattr(aqt, "mw", mw, raising=False)
        monkeypatch.setattr(
            "aqt.rwkv_scheduler.reschedule_rwkv_review_cards_with_progress",
            reschedule,
        )

        data = (
            decks_pb2.DeckId(did=deck_id).SerializeToString()
            if deck_id is not None
            else b""
        )
        with app.test_request_context(data=data):
            assert reschedule_rwkv_review_cards() == b""

        assert calls == [(mw, deck_id)]


def test_web_page_backend_request_counts_as_collection_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aqt
    from aqt.mediasrv import _extract_collection_post_request, app, post_handlers
    from aqt.taskman import TaskManager

    mw = SimpleNamespace(col=object())
    mw.weakref = lambda: mw  # type: ignore[attr-defined]
    mw.taskman = TaskManager(mw)  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", mw, raising=False)
    busy_during_request: list[bool] = []

    def handler() -> bytes:
        busy_during_request.append(mw.taskman.collection_busy())
        raise RuntimeError("the count must drop after a failure too")

    monkeypatch.setitem(post_handlers, "clankiTestRequest", handler)
    with app.test_request_context(method="POST"):
        _extract_collection_post_request("clankiTestRequest")()

    assert busy_during_request == [True]
    assert not mw.taskman.collection_busy()


def _card_stats_with_two_reviews() -> Any:
    from anki.stats_pb2 import CardStatsResponse

    response = CardStatsResponse()
    response.memory_state.stability = 30.0
    # newest first; a manual entry (no answer button) on top
    manual = response.revlog.add(time=300, button_chosen=0)
    manual.memory_state.stability = 30.0
    latest = response.revlog.add(time=200, button_chosen=3)
    latest.memory_state.stability = 30.0
    older = response.revlog.add(time=100, button_chosen=3)
    older.memory_state.stability = 12.0
    return response


@pytest.mark.parametrize("has_curve", [True, False])
def test_card_info_gets_rwkv_curves_own_curve_and_s90(
    monkeypatch: pytest.MonkeyPatch, has_curve: bool
) -> None:
    """Pins spec/ui.md#ui.card-info-rwkv-curve and
    #ui.card-info-one-algorithm"""
    import aqt.rwkv_scheduler as rwkv
    from aqt.mediasrv import _add_rwkv_curve

    curve = rwkv.RwkvCardCurve(
        elapsed_days=(0.0, 1.0), recall=(1.0, 0.8), s90=0.4, current_recall=0.93
    )
    elapsed: list[float | None] = []

    def card_info_curve(
        reviewer: object, card: object, *, elapsed_days: float | None = None
    ) -> object:
        elapsed.append(elapsed_days)
        return curve if has_curve else None

    monkeypatch.setattr(rwkv, "rwkv_review_enabled", lambda reviewer, card: True)
    monkeypatch.setattr(rwkv, "rwkv_card_info_curve", card_info_curve)
    monkeypatch.setattr("aqt.mediasrv.time.time", lambda: 200 + 2 * 86_400)
    response = _card_stats_with_two_reviews()

    _add_rwkv_curve(response, object(), object())

    assert response.HasField("rwkv_curve")
    if has_curve:
        assert list(response.rwkv_curve.elapsed_days) == [0.0, 1.0]
        assert list(response.rwkv_curve.recall) == pytest.approx([1.0, 0.8])
        assert response.rwkv_curve.s90 == pytest.approx(0.4)
        # the card's R: the curve two days after the latest answered review
        # (spec ui.card-info-one-algorithm)
        assert response.rwkv_curve.current_recall == pytest.approx(0.93)
        assert elapsed == [pytest.approx(2.0)]
        # the latest review shows the drawn curve's S90
        assert response.revlog[1].memory_state.stability == pytest.approx(0.4)
    else:
        assert not response.rwkv_curve.elapsed_days
        assert not response.rwkv_curve.HasField("s90")
        assert not response.rwkv_curve.HasField("current_recall")
        # no FSRS-7 value stands in for the missing curve
        assert not response.revlog[1].HasField("memory_state")
    # older reviews keep no FSRS-7 memory state; the newer manual entry and
    # the card's own state stay (card info decides which rows to show)
    assert not response.revlog[2].HasField("memory_state")
    assert response.revlog[0].memory_state.stability == 30.0
    assert response.memory_state.stability == 30.0


def test_card_info_has_no_rwkv_curve_for_other_algorithms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aqt.rwkv_scheduler as rwkv
    from aqt.mediasrv import _add_rwkv_curve

    monkeypatch.setattr(rwkv, "rwkv_review_enabled", lambda reviewer, card: False)
    response = _card_stats_with_two_reviews()

    _add_rwkv_curve(response, object(), object())

    assert not response.HasField("rwkv_curve")
    assert response.memory_state.stability == 30.0
    assert response.revlog[2].memory_state.stability == 12.0


class TestCheckDynamicRequestPermissions:
    """A missing Content-type header must abort(403), not raise KeyError."""

    def test_missing_content_type_header_aborts_403(self, monkeypatch) -> None:
        from unittest import mock

        from werkzeug.exceptions import Forbidden

        import aqt
        from aqt.mediasrv import _check_dynamic_request_permissions, app

        monkeypatch.setattr(aqt, "mw", mock.Mock(), raising=False)

        with app.test_request_context(method="POST"):
            with pytest.raises(Forbidden):
                _check_dynamic_request_permissions()


class TestEditorPageCSP:
    def test_editor_csp_does_not_block_user_embeds(self) -> None:
        csp = _editor_content_security_policy(port=12345)
        directives = _csp_directives(csp)

        assert directives["script-src"] == (
            "http://127.0.0.1:12345/_anki/ http://127.0.0.1:12345/_addons/"
        )
        assert "object-src" not in directives
        assert "frame-src" not in directives
        assert "child-src" not in directives
        assert "img-src" not in directives

    @pytest.mark.parametrize(
        "csp",
        [
            _legacy_editor_content_security_policy(port=12345),
            _untrusted_sveltekit_content_security_policy(12345, "'sha256-abc='"),
        ],
    )
    def test_editor_csp_blocks_navigation_and_framing(self, csp: str) -> None:
        """Field content must not navigate the editor webview via a form, and
        the editor itself must not be framed by other pages."""
        directives = _csp_directives(csp)
        assert directives["form-action"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"

    def test_sveltekit_editor_csp_allows_render_script_hash(self) -> None:
        csp = _untrusted_sveltekit_content_security_policy(12345, "'sha256-abc='")
        directives = _csp_directives(csp)
        assert directives["script-src"].split() == [
            "http://127.0.0.1:12345/_anki/",
            "http://127.0.0.1:12345/_app/",
            "http://127.0.0.1:12345/_addons/",
            "'sha256-abc='",
        ]


class TestTrustedPageCSP:
    """Internal pages are only shown top-level. Refusing frame ancestors keeps
    note HTML in the editor from embedding them in the API-access profile
    (GHSA-jw6j-j4mf-8jgm)."""

    SVELTEKIT_INDEX = (
        b'<html><head><meta http-equiv="content-security-policy" '
        b"content=\"script-src 'self' 'sha256-abc='\"></head></html>"
    )

    def _mock_mw(self, monkeypatch) -> mock.Mock:
        import aqt

        mw = mock.Mock()
        mw.mediaServer.getPort.return_value = 12345
        monkeypatch.setattr(aqt, "mw", mw, raising=False)
        return mw

    def _serve_builtin(self, monkeypatch, request: BundledFileRequest, data: bytes):
        from aqt import mediasrv

        self._mock_mw(monkeypatch)
        monkeypatch.setattr(mediasrv, "_builtin_data", lambda path: data)
        with mediasrv.app.test_request_context():
            return _handle_builtin_file_request(request)

    def test_trusted_sveltekit_page_refuses_framing(self, monkeypatch) -> None:
        request = BundledFileRequest(
            "sveltekit/index.html", sveltekit_route="deck-options"
        )
        resp = self._serve_builtin(monkeypatch, request, self.SVELTEKIT_INDEX)
        assert _get_csp(resp) == TRUSTED_PAGE_CSP
        assert b"content-security-policy" not in resp.get_data()

    @pytest.mark.parametrize("route", ["editor", "image-occlusion"])
    def test_untrusted_sveltekit_page_refuses_framing(
        self, monkeypatch, route: str
    ) -> None:
        request = BundledFileRequest("sveltekit/index.html", sveltekit_route=route)
        resp = self._serve_builtin(monkeypatch, request, self.SVELTEKIT_INDEX)
        csp = _get_csp(resp)
        assert csp is not None
        directives = _csp_directives(csp)
        assert directives["form-action"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"
        assert "'sha256-abc='" in directives["script-src"]

    def test_bundled_html_page_refuses_framing(self, monkeypatch) -> None:
        request = BundledFileRequest("pages/deckconfig.html")
        resp = self._serve_builtin(monkeypatch, request, b"<html></html>")
        assert _get_csp(resp) == TRUSTED_PAGE_CSP

    def test_bundled_asset_has_no_csp(self, monkeypatch) -> None:
        request = BundledFileRequest("js/foo.js")
        resp = self._serve_builtin(monkeypatch, request, b"console.log(1)")
        assert _get_csp(resp) is None

    @pytest.mark.parametrize(
        "context", [PageContext.REVIEWER, PageContext.PREVIEWER, PageContext.UNKNOWN]
    )
    def test_legacy_page_refuses_framing(self, monkeypatch, context) -> None:
        from aqt.mediasrv import app

        mw = self._mock_mw(monkeypatch)
        mw.mediaServer.get_page.return_value = LegacyPage("<html></html>", context)
        with app.test_request_context("/_anki/legacyPageData?id=1"):
            resp = legacy_page_data()
        assert _get_csp(resp) == TRUSTED_PAGE_CSP

    def test_legacy_editor_page_gets_editor_csp(self, monkeypatch) -> None:
        from aqt.mediasrv import app

        mw = self._mock_mw(monkeypatch)
        mw.mediaServer.get_page.return_value = LegacyPage(
            "<html></html>", PageContext.EDITOR
        )
        with app.test_request_context("/_anki/legacyPageData?id=1"):
            resp = legacy_page_data()
        assert _get_csp(resp) == _legacy_editor_content_security_policy(12345)


class TestCardStats:
    @pytest.mark.parametrize(
        "deck_config",
        [
            {"id": 1, "rwkvReviewInstantOrderEnabled": True},
            {
                "id": 1,
                "other": {
                    "jschoreels.fsrs": {
                        "rwkv_review_instant_order_enabled": True,
                    },
                },
            },
        ],
    )
    def test_card_info_includes_rwkv_diagnostics_without_reviewer_cache(
        self, monkeypatch: pytest.MonkeyPatch, deck_config: dict[str, object]
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt.mediasrv import app, card_stats
        from aqt.rwkv_scheduler import (
            RwkvIntervalOverride,
            RwkvReviewPrediction,
            set_reviewer_backend,
        )

        card = SimpleNamespace(id=123, did=10)
        response = CardStatsResponse(card_id=123)

        class Backend:
            def predict_review(
                self,
                *,
                reviewer: object,
                card: object,
            ) -> RwkvReviewPrediction:
                return RwkvReviewPrediction(
                    retrievability=0.61,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=4,
                        easy=8,
                    ),
                )

            def review_answered(
                self,
                *,
                reviewer: object,
                card: object,
                ease: int,
            ) -> None:
                pass

        class RawBackend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Decks:
            def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
                assert deck_id == 10
                return deck_config

        class Collection:
            _backend = RawBackend()
            decks = Decks()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        collection = Collection()
        mw = SimpleNamespace(col=collection)
        reviewer = SimpleNamespace(mw=mw)
        mw.reviewer = reviewer

        previous = set_reviewer_backend(Backend())
        try:
            monkeypatch.setattr(aqt, "mw", mw)
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            set_reviewer_backend(previous)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert [(row.label, row.value) for row in output.extra_rows] == [
            ("RWKV computed R", "61%"),
        ]

    def test_card_info_reports_rwkv_unavailable_when_backend_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt.mediasrv import app, card_stats
        from aqt.rwkv_scheduler import set_reviewer_backend

        card = SimpleNamespace(id=123, did=10)
        response = CardStatsResponse(card_id=123)
        response.memory_state.stability = 1.0

        class RawBackend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Decks:
            def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
                assert deck_id == 10
                return {"id": 1, "rwkvReviewInstantOrderEnabled": True}

        class Collection:
            _backend = RawBackend()
            decks = Decks()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
        monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
        monkeypatch.setattr("aqt.rwkv_scheduler.embedded_rwkv_model_path", lambda: None)
        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=Collection()))
        previous = set_reviewer_backend(None)
        try:
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            set_reviewer_backend(previous)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert [(row.label, row.value) for row in output.extra_rows] == [
            ("RWKV computed R", "Calculating…"),
        ]

    def test_card_info_hook_can_append_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt import gui_hooks
        from aqt.browser.card_info import CardInfoRow
        from aqt.mediasrv import app, card_stats

        card = object()
        response = CardStatsResponse(card_id=123)

        class Backend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Collection:
            _backend = Backend()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        def add_row(rows: list[CardInfoRow], hook_card: object) -> None:
            assert hook_card is card
            rows.append(CardInfoRow(label="Dynamic DR", value="0.71"))

        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=Collection()))
        gui_hooks.card_info_will_add_rows.append(add_row)
        try:
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            gui_hooks.card_info_will_add_rows.remove(add_row)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert len(output.extra_rows) == 1
        assert output.extra_rows[0].label == "Dynamic DR"
        assert output.extra_rows[0].value == "0.71"

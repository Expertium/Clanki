# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The preset routing key is part of the RWKV replay key, the identity of
every stored state cache: its value must not move. These tests pin the key
read with two backend calls for all decks to the key read deck by deck."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki.collection import Collection
from aqt import rwkv_scheduler as rwkv


def _reference_routing_key(reviewer: Any) -> list[list[object]]:
    """The key as it was read before: one config read per deck."""

    routing: list[list[object]] = []
    for raw_deck in reviewer.mw.col.decks.all_names_and_ids():
        config = rwkv._deck_config_for_deck_id(reviewer, raw_deck.id)
        config_id = config.get("id") if isinstance(config, dict) else None
        routing.append([raw_deck.id, config_id if isinstance(config_id, int) else None])
    return sorted(routing, key=lambda item: item[0])


def _collection_with_every_kind_of_deck(path: Path) -> Collection:
    col = Collection(str(path / "collection.anki2"))
    other = col.decks.add_config_returning_id("Other")
    for name, config_id in (("A", None), ("B", other), ("B::C", other)):
        deck_id = col.decks.id(name)
        assert deck_id
        if config_id is not None:
            deck = col.decks.get(deck_id)
            assert deck
            deck["conf"] = config_id
            col.decks.save(deck)
    col.decks.new_filtered("Filtered")
    return col


def _filtered_deck_id(col: Collection) -> int:
    deck_id = col.decks.id_for_name("Filtered")
    assert deck_id
    return int(deck_id)


def test_the_routing_key_read_for_all_decks_is_the_key_read_deck_by_deck(
    tmp_path: Path,
) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        key = rwkv._rwkv_preset_routing_config_key(reviewer)
        assert key == _reference_routing_key(reviewer)
        # the filtered deck routes to itself, the others to their presets:
        # the default one and "Other"
        filtered = _filtered_deck_id(col)
        assert [filtered, filtered] in key
        assert len({config_id for _, config_id in key}) == 3
    finally:
        col.close()


def test_a_deck_whose_preset_is_gone_routes_as_before(tmp_path: Path) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        deck_id = col.decks.id("A")
        assert deck_id
        deck = col.decks.get(deck_id)
        assert deck
        # the backend falls back to the default preset for a missing one
        deck["conf"] = 999_999
        col.decks.save(deck)
        saved = col.decks.get(deck_id)
        assert saved and int(saved["conf"]) == 999_999
        assert deck_id not in rwkv._deck_config_ids_of_decks(col.decks)
        key = rwkv._rwkv_preset_routing_config_key(reviewer)
        assert key == _reference_routing_key(reviewer)
        assert [deck_id, 1] in key
    finally:
        col.close()


def test_the_routing_key_takes_two_backend_calls_for_all_decks(
    tmp_path: Path,
) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        for index in range(20):
            col.decks.id(f"More::{index}")
        calls = 0
        real = col._backend._run_command

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        col._backend._run_command = counted  # type: ignore[method-assign]
        rwkv._rwkv_preset_routing_config_key(reviewer)
        # the deck list, the presets, the decks
        assert calls == 3
    finally:
        col.close()


def test_decks_without_the_bulk_reads_are_read_one_by_one() -> None:
    configs = {10: {"id": 7}, 11: {"id": 11}}
    decks = SimpleNamespace(
        all_names_and_ids=lambda: [SimpleNamespace(id=11), SimpleNamespace(id=10)],
        config_dict_for_deck_id=lambda deck_id: configs[deck_id],
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=decks)))
    assert rwkv._rwkv_preset_routing_config_key(reviewer) == [[10, 7], [11, 11]]


def test_the_configs_read_for_all_decks_are_the_configs_read_deck_by_deck(
    tmp_path: Path,
) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        configs = rwkv._deck_configs_of_decks(reviewer)
        for deck in col.decks.all():
            assert configs[deck["id"]] == rwkv._deck_config_for_deck_id(
                reviewer, deck["id"]
            )
        # a normal deck's preset has `dyn` False; a filtered deck is its own
        assert configs[1]["dyn"] is False
        assert "conf" not in configs[_filtered_deck_id(col)]
        # each deck gets its own dict, so a caller may change one
        assert configs[col.decks.id("B")] is not configs[col.decks.id("B::C")]
    finally:
        col.close()


def test_a_deck_whose_preset_is_gone_is_left_to_the_deck_by_deck_read(
    tmp_path: Path,
) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        deck_id = col.decks.id("A")
        assert deck_id
        deck = col.decks.get(deck_id)
        assert deck
        deck["conf"] = 999_999
        col.decks.save(deck)
        assert deck_id not in rwkv._deck_configs_of_decks(reviewer)
        config = rwkv._deck_config_for_deck_id(reviewer, deck_id)
        assert isinstance(config, dict) and config["id"] == 1
    finally:
        col.close()


def test_the_deck_list_count_scopes_take_three_backend_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    col = _collection_with_every_kind_of_deck(tmp_path)
    try:
        for index in range(20):
            col.decks.id(f"More::{index}")
        deck_b = col.decks.get(col.decks.id("B"))
        assert deck_b
        other = int(deck_b["conf"])
        # RWKV-Instant on for the "Other" preset only: "B" and its subtree
        # are one scope
        monkeypatch.setattr(
            rwkv,
            "_rwkv_review_instant_order_enabled",
            lambda config: config["id"] == other,
        )
        mw = SimpleNamespace(col=col)
        tree = col.sched.deck_due_tree()
        calls = 0
        real = col._backend._run_command

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        col._backend._run_command = counted  # type: ignore[method-assign]
        scopes = rwkv.deck_browser_rwkv_count_scope_ids(mw, tree)
        assert scopes == (deck_b["id"],)
        # the current deck, the presets, the decks
        assert calls == 3
    finally:
        col.close()

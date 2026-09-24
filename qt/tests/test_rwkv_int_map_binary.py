# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The int maps of the RWKV state cache (previous review id, previous
interval and review count per card) are stored in a fixed binary layout: a
little-endian u32 count, then each (key, value) as two little-endian i64,
in key order. Stored caches are read back with it, so its bytes must not
move. These tests pin the encoder and decoder to the entry-by-entry
reference below."""

from __future__ import annotations

import io
import random
import struct

import pytest

from aqt import rwkv_scheduler as rwkv


def _reference_encode(values: dict[int, int]) -> bytes:
    out = struct.pack("<I", len(values))
    for key, value in sorted(values.items()):
        out += struct.pack("<q", key) + struct.pack("<q", value)
    return out


def _reference_decode(data: bytes) -> dict[int, int]:
    (count,) = struct.unpack_from("<I", data, 0)
    values: dict[int, int] = {}
    for index in range(count):
        key, value = struct.unpack_from("<qq", data, 4 + 16 * index)
        values[key] = value
    return values


def _random_map(size: int, seed: int) -> dict[int, int]:
    rng = random.Random(seed)
    values: dict[int, int] = {}
    while len(values) < size:
        key = rng.choice(
            [rng.randrange(-(2**63), 2**63), rng.randrange(10**12, 2 * 10**12)]
        )
        values[key] = rng.choice(
            [rng.randrange(-(2**63), 2**63), rng.randrange(0, 40_000), 0, -1]
        )
    return values


@pytest.mark.parametrize("size", [0, 1, 2, 8191, 8192, 8193, 20_000])
def test_the_int_map_bytes_are_the_reference_bytes(size: int) -> None:
    values = _random_map(size, seed=size)
    encoded = rwkv._encode_int_map_binary(values)
    assert encoded == _reference_encode(values)
    decoded = rwkv._decode_int_map_binary(encoded)
    assert decoded == values
    # the decoder keeps the stored order, as the entry-by-entry reader did
    assert list(decoded.items()) == list(_reference_decode(encoded).items())


def test_an_int_map_written_to_a_file_has_the_same_bytes() -> None:
    values = _random_map(20_000, seed=7)
    stream = io.BytesIO()
    rwkv._write_int_map(stream, values)
    assert stream.getvalue() == _reference_encode(values)


def test_a_repeated_key_keeps_its_last_value_in_first_place() -> None:
    data = struct.pack("<I", 3) + struct.pack("<6q", 5, 1, 2, 2, 5, 3)
    decoded = rwkv._decode_int_map_binary(data)
    assert list(decoded.items()) == list(_reference_decode(data).items())
    assert list(decoded.items()) == [(5, 3), (2, 2)]


def test_a_truncated_int_map_is_refused() -> None:
    encoded = rwkv._encode_int_map_binary(_random_map(10, seed=3))
    with pytest.raises(ValueError, match="truncated RWKV cache binary data"):
        rwkv._decode_int_map_binary(encoded[:-1])
    with pytest.raises(ValueError, match="trailing RWKV cache binary data"):
        rwkv._decode_int_map_binary(encoded + b"\0")


def test_an_out_of_range_value_is_refused() -> None:
    with pytest.raises(struct.error):
        rwkv._encode_int_map_binary({1: 2**63})

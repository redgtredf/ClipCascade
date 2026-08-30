"""UTF-8-safe fragmentation and fragment-metadata validation tests."""

import random

import pytest

from core.fragment_utils import (
    MAX_TOTAL_FRAGMENTS_FLOOR,
    MAX_TOTAL_FRAGMENTS_HARD,
    is_valid_fragment_metadata,
    max_total_fragments,
    utf8_safe_chunks,
)

FRAGMENT_SIZE = 15360  # matches core/constants.py FRAGMENT_SIZE


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


# --- utf8_safe_chunks: correctness ---


def test_ascii_text_splits_identically_to_naive_byte_slicing():
    text = "hello world " * 3000
    chunks = utf8_safe_chunks(text, FRAGMENT_SIZE)
    assert "".join(chunks) == text
    assert all(_byte_len(c) <= FRAGMENT_SIZE for c in chunks)
    # Pure ASCII: no boundary adjustment needed.
    naive = [
        text.encode("utf-8")[i : i + FRAGMENT_SIZE].decode("utf-8")
        for i in range(0, _byte_len(text), FRAGMENT_SIZE)
    ]
    assert chunks == naive


def test_multibyte_characters_survive_all_boundaries():
    # Emoji are 4 bytes; CJK 3 bytes. The old implementation dropped every
    # sequence straddling a fragment boundary.
    text = "😀🎉👍_after_日本語テキスト_" * 5000
    chunks = utf8_safe_chunks(text, FRAGMENT_SIZE)
    assert "".join(chunks) == text
    assert all(_byte_len(c) <= FRAGMENT_SIZE for c in chunks)


def test_boundary_cut_lands_between_codepoints():
    # Craft text so a 4-byte emoji would straddle each 16-byte boundary.
    text = ("a" * 13 + "😀") * 1000
    chunks = utf8_safe_chunks(text, 16)
    assert "".join(chunks) == text
    for chunk in chunks:
        chunk.encode("utf-8")  # strict decode: never mid-codepoint


def test_round_trip_random_mixed_script_text():
    rng = random.Random(1234)
    alphabet = ["a", "7", " ", "é", "Ж", "中", "😀", "🇮🇳", "\n"]
    for _ in range(50):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5000)))
        size = rng.randint(4, 200)
        chunks = utf8_safe_chunks(text, size)
        assert "".join(chunks) == text
        assert all(_byte_len(c) <= size for c in chunks)


def test_empty_text_returns_empty_list():
    assert utf8_safe_chunks("", FRAGMENT_SIZE) == []


def test_single_fragment_for_small_payload():
    assert utf8_safe_chunks("hello", FRAGMENT_SIZE) == ["hello"]


def test_minimum_size_fits_one_codepoint_per_fragment():
    # size=4 exactly holds a 4-byte emoji; every fragment is one codepoint.
    chunks = utf8_safe_chunks("😀😀😀", 4)
    assert chunks == ["😀", "😀", "😀"]


def test_invalid_size_rejected():
    for bad_size in [0, 1, 3, -5, 2.5, None]:
        with pytest.raises(ValueError):
            utf8_safe_chunks("abc", bad_size)


# --- max_total_fragments ---


def test_no_limit_yields_hard_cap():
    for unlimited in [None, 0, -1, -100]:
        assert max_total_fragments(FRAGMENT_SIZE, unlimited) == MAX_TOTAL_FRAGMENTS_HARD


def test_positive_limit_derives_cap_with_margin():
    # 1 MiB limit -> ceil(1048576 / 15360) + 16 = 69 + 16 = 85 -> floored to 4096.
    assert (
        max_total_fragments(FRAGMENT_SIZE, 1024 * 1024)
        == MAX_TOTAL_FRAGMENTS_FLOOR
    )
    # Large limit: 2 GiB -> ceil(2147483648 / 15360) + 16, capped by the hard ceiling.
    derived = max_total_fragments(FRAGMENT_SIZE, 2 * 1024 * 1024 * 1024)
    assert derived == MAX_TOTAL_FRAGMENTS_HARD


def test_derived_cap_sits_between_floor_and_hard_cap():
    # ~200 MiB limit -> ceil(209715200 / 15360) + 16 = 13654 + 16 = 13670.
    assert max_total_fragments(FRAGMENT_SIZE, 200 * 1024 * 1024) == 13670


def test_bool_is_not_treated_as_positive_limit():
    assert max_total_fragments(FRAGMENT_SIZE, True) == MAX_TOTAL_FRAGMENTS_HARD


# --- is_valid_fragment_metadata ---


def test_valid_metadata_accepted():
    assert is_valid_fragment_metadata(10, 0)
    assert is_valid_fragment_metadata(10, 9)
    assert is_valid_fragment_metadata(1, 0)


def test_non_integer_metadata_rejected():
    assert not is_valid_fragment_metadata(10.5, 0)
    assert not is_valid_fragment_metadata(10, 1.0)
    assert not is_valid_fragment_metadata(None, 0)
    assert not is_valid_fragment_metadata("10", 0)
    assert not is_valid_fragment_metadata(True, 0)
    assert not is_valid_fragment_metadata(float("nan"), 0)


def test_out_of_range_metadata_rejected():
    assert not is_valid_fragment_metadata(0, 0)
    assert not is_valid_fragment_metadata(-10, 0)
    assert not is_valid_fragment_metadata(10, -1)
    assert not is_valid_fragment_metadata(10, 10)
    assert not is_valid_fragment_metadata(10, 10**9)

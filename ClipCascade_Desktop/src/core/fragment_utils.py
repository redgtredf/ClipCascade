"""
Shared, dependency-free helpers for P2P clipboard fragmentation.

Used by p2p_manager to:
- split UTF-8 text into fragments without corrupting multi-byte sequences
  (the previous naive byte slicing silently dropped characters that
  straddled a fragment boundary), and
- validate remote fragment metadata before allocating memory for
  reassembly (a peer could previously request an arbitrary number of
  pre-allocated fragments).
"""

import math

# Absolute ceiling on fragments accepted for a single stream, regardless of
# any configured local size limit. At the default FRAGMENT_SIZE of 15 KiB
# this permits payloads up to ~1 GiB while keeping the pre-allocation for
# reassembly bounded (an unclamped peer-supplied totalFragments could
# previously trigger a multi-GB allocation).
MAX_TOTAL_FRAGMENTS_HARD = 65536

# Lower bound so that even a small configured limit still accepts ordinary
# payloads; the size-based rejection itself is handled separately by the
# combinedRawPayloadSizeInBytes check.
MAX_TOTAL_FRAGMENTS_FLOOR = 4096

# Safety margin over ceil(limit / fragment_size) for the derived cap.
MAX_TOTAL_FRAGMENTS_MARGIN = 16

# UTF-8 continuation bytes are of the form 0b10xxxxxx.
_UTF8_CONTINUATION_MASK = 0xC0
_UTF8_CONTINUATION_PATTERN = 0x80


def utf8_safe_chunks(text: str, size: int) -> list:
    """
    Split `text` into fragments of at most `size` UTF-8 bytes each, where
    every fragment is a valid, standalone UTF-8 string and the fragments
    concatenated in order reproduce `text` exactly.

    Args:
        text: The string to fragment.
        size: Maximum byte size of each fragment (must be >= 4 so a single
            codepoint always fits in one fragment and the boundary back-off
            always makes progress).

    Returns:
        list[str]: The fragments. Empty list for empty input.

    Raises:
        ValueError: If `size` is too small to guarantee progress.
    """
    if not isinstance(size, int) or size < 4:
        # A UTF-8 codepoint is at most 4 bytes, so size >= 4 guarantees the
        # boundary back-off below always makes progress.
        raise ValueError("fragment size must be an integer >= 4")

    data = text.encode("utf-8")
    total = len(data)
    chunks: list = []
    start = 0

    while start < total:
        end = min(start + size, total)
        if end < total:
            # Back off over continuation bytes so the cut lands between
            # codepoints.
            while end > start and (data[end] & _UTF8_CONTINUATION_MASK) == _UTF8_CONTINUATION_PATTERN:
                end -= 1
        # Strict decode: safe by construction, and loud if ever violated.
        chunks.append(data[start:end].decode("utf-8"))
        start = end

    return chunks


def max_total_fragments(fragment_size: int, local_limit_bytes=None) -> int:
    """
    Upper bound on the number of fragments a single inbound stream may
    declare before it is rejected.

    When a positive local clipboard size limit is configured, the bound is
    derived from it (so legitimate transfers under the limit always fit);
    otherwise the hard ceiling applies.
    """
    if (
        isinstance(local_limit_bytes, (int, float))
        and not isinstance(local_limit_bytes, bool)
        and local_limit_bytes > 0
    ):
        derived = math.ceil(local_limit_bytes / fragment_size) + MAX_TOTAL_FRAGMENTS_MARGIN
        return max(MAX_TOTAL_FRAGMENTS_FLOOR, min(derived, MAX_TOTAL_FRAGMENTS_HARD))
    return MAX_TOTAL_FRAGMENTS_HARD


def is_valid_fragment_metadata(total_fragments, fragment_index) -> bool:
    """
    True when total_fragments / fragment_index are well-formed integers
    describing a position inside the stream. Rejects JSON floats, bools
    (which are ints in Python), and out-of-range indices.
    """
    if type(total_fragments) is not int or type(fragment_index) is not int:
        return False
    if total_fragments < 1:
        return False
    return 0 <= fragment_index < total_fragments

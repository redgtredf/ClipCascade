'use strict';

// Shared, dependency-free helpers for P2P clipboard fragmentation.
// Used by StartForegroundService.js to:
// - split UTF-8 text into fragments without corrupting multi-byte sequences
//   (the previous naive byte slicing turned characters straddling a fragment
//   boundary into U+FFFD replacement characters), and
// - validate remote fragment metadata before allocating memory for
//   reassembly (a peer could previously request an arbitrary number of
//   pre-allocated fragments).

// Absolute ceiling on fragments accepted for a single stream, regardless of
// any configured local size limit. At the default FRAGMENT_SIZE of 15 KiB
// this permits payloads up to ~1 GiB while keeping the pre-allocation for
// reassembly bounded (an unclamped peer-supplied totalFragments could
// previously trigger a multi-GB allocation).
const MAX_TOTAL_FRAGMENTS_HARD = 65536;

// Lower bound so that even a small configured limit still accepts ordinary
// payloads; the size-based rejection itself is handled separately by the
// combinedRawPayloadSizeInBytes check.
const MAX_TOTAL_FRAGMENTS_FLOOR = 4096;

// Safety margin over ceil(limit / fragment_size) for the derived cap.
const MAX_TOTAL_FRAGMENTS_MARGIN = 16;

const UTF8_CONTINUATION_MASK = 0xc0;
const UTF8_CONTINUATION_PATTERN = 0x80;

/**
 * Split `text` into fragments of at most `size` UTF-8 bytes each, where
 * every fragment is a valid, standalone UTF-8 string and the fragments
 * concatenated in order reproduce `text` exactly.
 *
 * @param {string} text the string to fragment
 * @param {number} size maximum byte size of each fragment (>= 4)
 * @returns {string[]} the fragments (empty array for empty input)
 * @throws {Error} if size is too small to guarantee progress
 */
function utf8SafeChunks(text, size) {
  if (!Number.isInteger(size) || size < 4) {
    // A UTF-8 codepoint is at most 4 bytes, so size >= 4 guarantees the
    // boundary back-off below always makes progress.
    throw new Error('fragment size must be an integer >= 4');
  }

  const data = new TextEncoder().encode(text);
  const total = data.length;
  const chunks = [];
  const decoder = new TextDecoder();
  let start = 0;

  while (start < total) {
    let end = Math.min(start + size, total);
    if (end < total) {
      // Back off over continuation bytes so the cut lands between codepoints.
      while (end > start && (data[end] & UTF8_CONTINUATION_MASK) === UTF8_CONTINUATION_PATTERN) {
        end -= 1;
      }
    }
    // Aligned chunks are valid UTF-8 by construction; TextDecoder throws
    // (non-fatal mode is not set) if that invariant is ever violated.
    chunks.push(decoder.decode(data.subarray(start, end)));
    start = end;
  }

  return chunks;
}

/**
 * Upper bound on the number of fragments a single inbound stream may declare
 * before it is rejected. When a positive local clipboard size limit is
 * configured, the bound is derived from it (so legitimate transfers under the
 * limit always fit); otherwise the hard ceiling applies.
 *
 * @param {number} fragmentSize maximum byte size of one fragment
 * @param {number|null} localLimitBytes configured local limit in bytes (or a
 *   non-positive/undefined value meaning "unlimited")
 * @returns {number}
 */
function maxTotalFragments(fragmentSize, localLimitBytes) {
  if (Number.isFinite(localLimitBytes) && localLimitBytes > 0) {
    const derived =
      Math.ceil(localLimitBytes / fragmentSize) + MAX_TOTAL_FRAGMENTS_MARGIN;
    return Math.max(
      MAX_TOTAL_FRAGMENTS_FLOOR,
      Math.min(derived, MAX_TOTAL_FRAGMENTS_HARD),
    );
  }
  return MAX_TOTAL_FRAGMENTS_HARD;
}

/**
 * True when totalFragments / fragmentIndex are well-formed integers
 * describing a position inside the stream. Rejects floats, NaN, and
 * out-of-range indices.
 *
 * @param {*} totalFragments
 * @param {*} fragmentIndex
 * @returns {boolean}
 */
function isValidFragmentMetadata(totalFragments, fragmentIndex) {
  if (!Number.isInteger(totalFragments) || !Number.isInteger(fragmentIndex)) {
    return false;
  }
  if (totalFragments < 1) {
    return false;
  }
  return fragmentIndex >= 0 && fragmentIndex < totalFragments;
}

module.exports = {
  MAX_TOTAL_FRAGMENTS_HARD,
  MAX_TOTAL_FRAGMENTS_FLOOR,
  MAX_TOTAL_FRAGMENTS_MARGIN,
  utf8SafeChunks,
  maxTotalFragments,
  isValidFragmentMetadata,
};

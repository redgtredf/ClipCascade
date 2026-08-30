/**
 * UTF-8-safe fragmentation and fragment-metadata validation tests.
 * Pure CommonJS module: also runnable standalone via `node` without Metro.
 */

const {
  MAX_TOTAL_FRAGMENTS_FLOOR,
  MAX_TOTAL_FRAGMENTS_HARD,
  utf8SafeChunks,
  maxTotalFragments,
  isValidFragmentMetadata,
} = require('../fragmentUtils');

const FRAGMENT_SIZE = 15360; // matches StartForegroundService.js

const byteLen = s => new TextEncoder().encode(s).length;

describe('utf8SafeChunks', () => {
  test('ascii text splits identically to naive byte slicing', () => {
    const text = 'hello world '.repeat(3000);
    const chunks = utf8SafeChunks(text, FRAGMENT_SIZE);
    expect(chunks.join('')).toBe(text);
    chunks.forEach(c => expect(byteLen(c)).toBeLessThanOrEqual(FRAGMENT_SIZE));
    const naive = [];
    const bytes = new TextEncoder().encode(text);
    for (let i = 0; i < bytes.length; i += FRAGMENT_SIZE) {
      naive.push(new TextDecoder().decode(bytes.slice(i, i + FRAGMENT_SIZE)));
    }
    expect(chunks).toEqual(naive);
  });

  test('multibyte characters survive all boundaries', () => {
    // The old implementation produced U+FFFD at every fragment boundary.
    const text = '😀🎉👍_after_日本語テキスト_'.repeat(5000);
    const chunks = utf8SafeChunks(text, FRAGMENT_SIZE);
    expect(chunks.join('')).toBe(text);
    chunks.forEach(c => expect(byteLen(c)).toBeLessThanOrEqual(FRAGMENT_SIZE));
  });

  test('boundary cut lands between codepoints', () => {
    const text = ('a'.repeat(13) + '😀').repeat(1000);
    const chunks = utf8SafeChunks(text, 16);
    expect(chunks.join('')).toBe(text);
  });

  test('empty text returns empty list', () => {
    expect(utf8SafeChunks('', FRAGMENT_SIZE)).toEqual([]);
  });

  test('minimum size fits one codepoint per fragment', () => {
    expect(utf8SafeChunks('😀😀😀', 4)).toEqual(['😀', '😀', '😀']);
  });

  test('invalid size rejected', () => {
    [0, 1, 3, -5, 2.5, null, undefined].forEach(badSize => {
      expect(() => utf8SafeChunks('abc', badSize)).toThrow();
    });
  });
});

describe('maxTotalFragments', () => {
  test('no limit yields hard cap', () => {
    [undefined, 0, -1, -100].forEach(unlimited => {
      expect(maxTotalFragments(FRAGMENT_SIZE, unlimited)).toBe(
        MAX_TOTAL_FRAGMENTS_HARD,
      );
    });
  });

  test('small positive limit is floored', () => {
    expect(maxTotalFragments(FRAGMENT_SIZE, 1024 * 1024)).toBe(
      MAX_TOTAL_FRAGMENTS_FLOOR,
    );
  });

  test('huge limit is capped by the hard ceiling', () => {
    expect(maxTotalFragments(FRAGMENT_SIZE, 2 ** 31)).toBe(
      MAX_TOTAL_FRAGMENTS_HARD,
    );
  });

  test('derived cap sits between floor and hard cap', () => {
    expect(maxTotalFragments(FRAGMENT_SIZE, 200 * 1024 * 1024)).toBe(13670);
  });
});

describe('isValidFragmentMetadata', () => {
  test('valid metadata accepted', () => {
    expect(isValidFragmentMetadata(10, 0)).toBe(true);
    expect(isValidFragmentMetadata(10, 9)).toBe(true);
    expect(isValidFragmentMetadata(1, 0)).toBe(true);
  });

  test('non-integer metadata rejected', () => {
    expect(isValidFragmentMetadata(10.5, 0)).toBe(false);
    expect(isValidFragmentMetadata(9.5, 0)).toBe(false);
    expect(isValidFragmentMetadata(null, 0)).toBe(false);
    expect(isValidFragmentMetadata('10', 0)).toBe(false);
    expect(isValidFragmentMetadata(undefined, 0)).toBe(false);
    expect(isValidFragmentMetadata(NaN, 0)).toBe(false);
    expect(isValidFragmentMetadata(Infinity, 0)).toBe(false);
  });

  test('out-of-range metadata rejected', () => {
    expect(isValidFragmentMetadata(0, 0)).toBe(false);
    expect(isValidFragmentMetadata(-10, 0)).toBe(false);
    expect(isValidFragmentMetadata(10, -1)).toBe(false);
    expect(isValidFragmentMetadata(10, 10)).toBe(false);
    expect(isValidFragmentMetadata(10, 10 ** 9)).toBe(false);
  });
});

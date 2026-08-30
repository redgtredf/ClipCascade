const { requireSecureServerUrl } = require('../transportSecurity');

describe('requireSecureServerUrl', () => {
  test('normalizes secure server URLs', () => {
    expect(requireSecureServerUrl(' HTTPS://Example.COM:8443/// ')).toBe(
      'https://example.com:8443',
    );
  });

  test.each([
    'http://example.com',
    'http://localhost:8080',
    'ws://example.com',
    'example.com',
    '',
  ])('rejects insecure or invalid URL %p', value => {
    expect(() => requireSecureServerUrl(value)).toThrow();
  });

  test('rejects embedded credentials', () => {
    expect(() => requireSecureServerUrl('https://user:pass@example.com')).toThrow(
      'must not contain credentials',
    );
  });

  test('rejects query strings and fragments', () => {
    expect(() => requireSecureServerUrl('https://example.com?a=b')).toThrow(
      'must not contain a query or fragment',
    );
    expect(() => requireSecureServerUrl('https://example.com/#x')).toThrow(
      'must not contain a query or fragment',
    );
  });
});

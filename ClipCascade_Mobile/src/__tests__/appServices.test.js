/**
 * Unit tests for the stateless services extracted from App.js
 * (see src/appServices.js). Network seams are faked via global.fetch;
 * pbkdf2 is the jest mock installed by jest.setup.js and configured
 * per-test.
 */

import { Buffer } from 'buffer';

import {
  convertToWebSocketUrl,
  fetchTimeout,
  getCSRFToken,
  hash,
  looksLikeSha3Hex,
  login,
  nextPollDelayMs,
  POLL_MAX_MS,
  POLL_MIN_MS,
  stringToSHA3_512LowercaseHex,
  validateSession,
} from '../appServices';
import { pbkdf2 } from '@react-native-module/pbkdf2';

const SHA3_TEST_VECTOR =
  '35755f4d41c8472523ccc03428f0cb57e10c3a16d24ccd71c936643db51cd6a8' +
  'd446d7027feb10e46ef635ed004ee782392ab3699e7eaa6e020c09eb8f912f76';

// --- pure helpers --------------------------------------------------------


test('looksLikeSha3Hex accepts only 128-char lowercase hex digests', () => {
  expect(looksLikeSha3Hex('a'.repeat(128))).toBe(true);
  expect(looksLikeSha3Hex('0123456789abcdef'.repeat(8))).toBe(true);
  expect(looksLikeSha3Hex('A'.repeat(128))).toBe(false);
  expect(looksLikeSha3Hex('a'.repeat(127))).toBe(false);
  expect(looksLikeSha3Hex('correct horse')).toBe(false);
  expect(looksLikeSha3Hex('')).toBe(false);
  expect(looksLikeSha3Hex(null)).toBe(false);
});


test('nextPollDelayMs snaps to the minimum on change and relaxes when idle', () => {
  expect(nextPollDelayMs(2000, true)).toBe(POLL_MIN_MS);
  expect(nextPollDelayMs(300, false)).toBe(450);
  expect(nextPollDelayMs(450, false)).toBe(675);
  expect(nextPollDelayMs(1500, false)).toBe(2250 > POLL_MAX_MS ? POLL_MAX_MS : 2250);
  expect(nextPollDelayMs(POLL_MAX_MS, false)).toBe(POLL_MAX_MS);
});


describe('convertToWebSocketUrl', () => {
  test('https becomes wss with the endpoint appended', async () => {
    expect(
      await convertToWebSocketUrl('https://example.com/', '/clipsocket'),
    ).toBe('wss://example.com/clipsocket');
  });

  test('http becomes ws', async () => {
    expect(await convertToWebSocketUrl('http://example.com', '/x')).toBe(
      'ws://example.com/x',
    );
  });

  test('no endpoint leaves the bare websocket url', async () => {
    expect(await convertToWebSocketUrl('https://example.com')).toBe(
      'wss://example.com',
    );
  });

  test('unsupported or invalid input throws', async () => {
    await expect(convertToWebSocketUrl('ftp://example.com')).rejects.toThrow(
      'Unsupported protocol',
    );
    await expect(convertToWebSocketUrl('')).rejects.toThrow('Invalid URL');
    await expect(convertToWebSocketUrl(null)).rejects.toThrow('Invalid URL');
  });
});


test('stringToSHA3_512LowercaseHex matches the reference vector', async () => {
  expect(await stringToSHA3_512LowercaseHex('ClipCascade-test-vector')).toBe(
    SHA3_TEST_VECTOR,
  );
});

// --- fetch seams ----------------------------------------------------------


describe('fetchTimeout', () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('passes the abort signal through and resolves the response', async () => {
    let seenSignal;
    global.fetch = jest.fn(async (_input, init) => {
      seenSignal = init.signal;
      return { ok: true, status: 200 };
    });

    const response = await fetchTimeout('https://example.com/health');
    expect(response.ok).toBe(true);
    expect(seenSignal).toBeDefined();
    expect(seenSignal.aborted).toBe(false);
  });

  test('aborts and rethrows when the server is too slow', async () => {
    global.fetch = jest.fn(
      (_input, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
          });
        }),
    );

    await expect(
      fetchTimeout('https://example.com/slow', {}, 20),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });
});


describe('validateSession', () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('OK body means a valid session', async () => {
    global.fetch = jest.fn(async () => ({
      ok: true,
      text: async () => 'OK',
    }));
    const [valid, message] = await validateSession({
      server_url: 'https://example.com',
    });
    expect(valid).toBe(true);
    expect(message).toContain('valid');
  });

  test('anything else is invalid', async () => {
    global.fetch = jest.fn(async () => ({
      ok: true,
      text: async () => '<html>login page</html>',
    }));
    const [valid] = await validateSession({
      server_url: 'https://example.com',
    });
    expect(valid).toBe(false);
  });
});


describe('getCSRFToken', () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('returns the token from the JSON body', async () => {
    global.fetch = jest.fn(async () => ({
      ok: true,
      json: async () => ({ token: 'csrf-123', headerName: 'X-CSRF-TOKEN' }),
    }));
    expect(await getCSRFToken({ server_url: 'https://example.com' })).toBe(
      'csrf-123',
    );
  });

  test('network failure resolves to an empty token', async () => {
    global.fetch = jest.fn(async () => {
      throw new Error('offline');
    });
    expect(await getCSRFToken({ server_url: 'https://example.com' })).toBe('');
  });
});

// --- key derivation --------------------------------------------------------


describe('hash (PBKDF2 key derivation)', () => {
  test('derives with username+password+salt salt, configured rounds, 32 bytes', async () => {
    pbkdf2.mockImplementation(
      (password, salt, rounds, keyLen, algo, callback) => {
        expect(password.equals(Buffer.from('raw-pass', 'utf8'))).toBe(true);
        expect(salt.equals(Buffer.from('userraw-passpepper', 'utf8'))).toBe(
          true,
        );
        expect(rounds).toBe(1234);
        expect(keyLen).toBe(32);
        expect(algo).toBe('sha256');
        callback(null, Buffer.alloc(32, 7));
      },
    );

    const data_s = {
      username: 'user',
      salt: 'pepper',
      hash_rounds: '1234',
    };
    const [ok, key, returnedData] = await hash(data_s, 'raw-pass');
    expect(ok).toBe(true);
    expect(key.equals(Buffer.alloc(32, 7))).toBe(true);
    expect(returnedData).toBe(data_s);
  });

  test('derivation errors come back as [false, error, data]', async () => {
    pbkdf2.mockImplementation((_p, _s, _r, _k, _a, callback) => {
      callback(new Error('native failure'));
    });
    const data_s = { username: 'u', salt: 's', hash_rounds: '10' };
    const [ok, error] = await hash(data_s, 'pw');
    expect(ok).toBe(false);
    // the rejection value is the callback's [false, Error, data] triple
    // (pre-existing shape; preserved by the extraction)
    expect(Array.isArray(error)).toBe(true);
    expect(error[0]).toBe(false);
    expect(error[1]).toBeInstanceOf(Error);
  });
});

// --- login -----------------------------------------------------------------


function loginPageResponse({ csrf = 'csrf-token', cookie = 'JSESSIONID=abc' } = {}) {
  const html =
    '<html><body><form>' +
    `<input name="_csrf" value="${csrf}"/>` +
    '</form></body></html>';
  return {
    ok: true,
    status: 200,
    text: async () => html,
    headers: { get: (name) => (name === 'set-cookie' ? cookie : null) },
  };
}

function jsonResponse(body, ok = true, status = 200) {
  return {
    ok,
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}


describe('login', () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
    jest.clearAllMocks();
  });

  const baseData = {
    server_url: 'https://example.com',
    username: 'user',
    cipher_enabled: 'false',
  };

  test('successful P2S login fills csrf, mode, maxsize and websocket url', async () => {
    const posts = [];
    global.fetch = jest.fn(async (url, init = {}) => {
      if (init.method === 'POST') {
        posts.push({ url, body: init.body });
        return { ok: true, status: 302 };
      }
      const path = String(url).replace('https://example.com', '');
      if (path === '/login') {
        return loginPageResponse();
      }
      if (path === '/whoami') {
        return jsonResponse({ username: 'user', role: 'USER' });
      }
      if (path === '/csrf-token') {
        return jsonResponse({ token: 'fresh-csrf' });
      }
      if (path === '/server-mode') {
        return jsonResponse({ mode: 'P2S' });
      }
      if (path === '/max-size') {
        return jsonResponse({ maxsize: 1048576 });
      }
      throw new Error('unexpected fetch ' + url);
    });

    const [ok, message, data_s] = await login(
      { ...baseData },
      'sha3-of-password',
    );
    expect(ok).toBe(true);
    expect(message).toContain('Login successful');
    expect(data_s.server_mode).toBe('P2S');
    expect(data_s.maxsize).toBe('1048576');
    expect(data_s.websocket_url).toBe('wss://example.com/clipsocket');
    expect(data_s.csrf_token).toBe('fresh-csrf');
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toContain('username=user');
    expect(posts[0].body).toContain('password=' + encodeURIComponent('sha3-of-password'));
    expect(posts[0].body).toContain('_csrf=csrf-token');
  });

  test('whoami mismatch fails the login instead of trusting the redirect', async () => {
    global.fetch = jest.fn(async (url, init = {}) => {
      if (init.method === 'POST') {
        return { ok: true, status: 302 };
      }
      const path = String(url).replace('https://example.com', '');
      if (path === '/login') {
        return loginPageResponse();
      }
      if (path === '/whoami') {
        return jsonResponse({ username: 'somebody-else' });
      }
      throw new Error('unexpected fetch ' + url);
    });

    const [ok, message] = await login({ ...baseData }, 'pw');
    expect(ok).toBe(false);
    expect(message).toContain('Login failed');
  });

  test('login page without a csrf token fails cleanly', async () => {
    global.fetch = jest.fn(async () => loginPageResponse({ csrf: '' }));
    const [ok, message] = await login({ ...baseData }, 'pw');
    expect(ok).toBe(false);
    expect(message).toContain('No CSRF token');
  });

  test('cipher logins derive and store the E2E key from the raw password', async () => {
    const { NativeModules } = require('react-native');
    pbkdf2.mockImplementation((_p, _s, _r, _k, _a, callback) => {
      callback(null, Buffer.alloc(32, 9));
    });

    global.fetch = jest.fn(async (url, init = {}) => {
      if (init.method === 'POST') {
        return { ok: true, status: 302 };
      }
      const path = String(url).replace('https://example.com', '');
      if (path === '/login') {
        return loginPageResponse();
      }
      if (path === '/whoami') {
        return jsonResponse({ username: 'user' });
      }
      if (path === '/csrf-token') {
        return jsonResponse({ token: 't' });
      }
      if (path === '/server-mode') {
        return jsonResponse({ mode: 'P2S' });
      }
      if (path === '/max-size') {
        return jsonResponse({ maxsize: 1 });
      }
      throw new Error('unexpected fetch ' + url);
    });

    const [ok, , data_s] = await login(
      { ...baseData, cipher_enabled: 'true' },
      'sha3-of-password',
      'raw-password',
    );
    expect(ok).toBe(true);
    expect(pbkdf2).toHaveBeenCalled();
    expect(NativeModules.NativeBridgeModule.storeE2EKey).toHaveBeenCalledWith(
      Buffer.alloc(32, 9).toString('base64'),
    );
    expect(data_s.hashed_password).toBe('');
  });

  test('legacy stored hash cannot re-derive: keystore key untouched', async () => {
    const { NativeModules } = require('react-native');
    global.fetch = jest.fn(async (url, init = {}) => {
      if (init.method === 'POST') {
        return { ok: true, status: 302 };
      }
      const path = String(url).replace('https://example.com', '');
      if (path === '/login') {
        return loginPageResponse();
      }
      if (path === '/whoami') {
        return jsonResponse({ username: 'user' });
      }
      if (path === '/csrf-token') {
        return jsonResponse({ token: 't' });
      }
      if (path === '/server-mode') {
        return jsonResponse({ mode: 'P2S' });
      }
      if (path === '/max-size') {
        return jsonResponse({ maxsize: 1 });
      }
      throw new Error('unexpected fetch ' + url);
    });

    const [ok] = await login(
      { ...baseData, cipher_enabled: 'true' },
      'legacy-sha3-hex',
      null,
    );
    expect(ok).toBe(true);
    expect(pbkdf2).not.toHaveBeenCalled();
    expect(
      NativeModules.NativeBridgeModule.storeE2EKey,
    ).not.toHaveBeenCalled();
  });
});

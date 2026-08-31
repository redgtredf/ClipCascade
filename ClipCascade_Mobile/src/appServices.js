/**
 * Stateless app services extracted from the App component: network helpers,
 * the login/session flow, key derivation and the sync-bridge poll cadence.
 * Everything here takes explicit parameters and returns values — no React
 * state — so it is unit-testable (see __tests__/appServices.test.js).
 */

import { NativeModules } from 'react-native';

import { pbkdf2 } from '@react-native-module/pbkdf2';
import { Buffer } from 'buffer';
import { sha3_512 } from 'js-sha3';
import { DOMParser } from 'react-native-html-parser';

import {
  CSRF_URL,
  FETCH_TIMEOUT,
  LOGIN_URL,
  MAXSIZE_URL,
  SERVER_MODE_URL,
  STUN_URL,
  VALIDATE_URL,
  WEBSOCKET_ENDPOINT,
  WEBSOCKET_ENDPOINT_P2P,
  WHOAMI_URL,
} from './appConfig';
const { requireSecureServerUrl } = require('./transportSecurity');

// A legacy 'password' in storage is a 128-char lowercase sha3-512 hex digest
// (older builds stored the hashed form). The raw password cannot be
// recovered from it.
export const looksLikeSha3Hex = value =>
  typeof value === 'string' &&
  value.length === 128 &&
  /^[0-9a-f]+$/.test(value);

// Adaptive sync-bridge poll: stay at 300ms while flags are changing (live
// status updates remain snappy), relax up to 2s while idle so the native
// bridge is not busy-polled at ~3.3 Hz forever.
export const POLL_MIN_MS = 300;
export const POLL_MAX_MS = 2000;
export const POLL_GROWTH = 1.5;
export const nextPollDelayMs = (currentDelayMs, flagsChanged) => {
  if (flagsChanged) {
    return POLL_MIN_MS;
  }
  return Math.min(POLL_MAX_MS, Math.round(currentDelayMs * POLL_GROWTH));
};

export const fetchTimeout = async (input, init, timeout_ms = FETCH_TIMEOUT) => {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout_ms);
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (e) {
    throw e;
  }
};

// Function to convert a server URL to a WebSocket URL
export const convertToWebSocketUrl = async (inputUrl, endpoint) => {
  if (!inputUrl || typeof inputUrl !== 'string') {
    throw new Error('Invalid URL provided');
  }

  inputUrl = inputUrl.trim().replace(/\/+$/, '').toLowerCase(); // Remove trailing slashes and convert to lowercase

  let wsUrl;

  if (inputUrl.startsWith('https://')) {
    wsUrl = inputUrl.replace('https://', 'wss://');
  } else if (inputUrl.startsWith('http://')) {
    wsUrl = inputUrl.replace('http://', 'ws://');
  } else {
    throw new Error(`Unsupported protocol in URL: ${inputUrl}`);
  }

  if (endpoint != null) {
    wsUrl += endpoint;
    wsUrl = wsUrl.replace(/\/+$/, '');
  }

  return wsUrl;
};

// Generate a PBKDF2 hash to create an encryption key.
export const hash = async (data_s, password_s) => {
  try {
    const salt = [data_s.username, password_s, data_s.salt].join('');

    const derivedKey = await new Promise((resolve, reject) => {
      pbkdf2(
        Buffer.from(password_s, 'utf8'),
        Buffer.from(salt, 'utf8'),
        Number(data_s.hash_rounds),
        32,
        'sha256',
        (err, key) => {
          if (err) {
            reject([false, err, data_s]);
          } else {
            resolve([true, key, data_s]);
          }
        },
      );
    });

    return derivedKey;
  } catch (error) {
    return [false, error, data_s];
  }
};

// SHA3-512 hash for password
export const stringToSHA3_512LowercaseHex = async input => {
  return sha3_512(input).toLowerCase();
};

// Function to validate session
export const validateSession = async data_s => {
  try {
    const response = await fetchTimeout(data_s.server_url + VALIDATE_URL, {
      method: 'GET',
    });

    if (response.ok && (await response.text()) === 'OK') {
      return [true, 'Cookie authentication is valid.'];
    } else {
      return [false, 'Cookie authentication is not valid.'];
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      return [false, 'Error: Request timed out'];
    }
    return [false, error];
  }
};

// Get CSRF token
export const getCSRFToken = async data_s => {
  try {
    const response = await fetchTimeout(data_s.server_url + CSRF_URL, {
      method: 'GET',
    });

    const responseData = await response.json();
    if (response.ok) {
      return responseData.token || '';
    }
  } catch (error) {
    return '';
  }
};

// Login
export const login = async (data_s, password_s, rawPasswordForKey = null) => {
  try {
    data_s.server_url = requireSecureServerUrl(data_s.server_url);

    // 1. Fetch the login page to get CSRF token and initial cookie
    const loginPageResponse = await fetchTimeout(
      data_s.server_url + LOGIN_URL,
      {
        method: 'GET',
      },
    );

    if (!loginPageResponse.ok) {
      const msg = `Failed to fetch login page: ${loginPageResponse.status}`;
      return [false, msg, data_s];
    }

    // parse HTML to get _csrf using react-native-html-parser
    const htmlText = await loginPageResponse.text();

    // Create a new DOM Parser instance
    const parser = new DOMParser();
    // Parse HTML
    const doc = parser.parseFromString(htmlText, 'text/html');

    // Find <input> elements and look for name="_csrf"
    const inputElements = doc.getElementsByTagName('input');
    let csrfToken = '';

    for (let i = 0; i < inputElements.length; i++) {
      const nameAttr = inputElements[i].getAttribute('name');
      if (nameAttr === '_csrf') {
        csrfToken = inputElements[i].getAttribute('value');
        break;
      }
    }

    if (csrfToken === '') {
      return [false, 'No CSRF token found in login page', data_s];
    }

    // 2. Retrieve the cookie(s) from the "Set-Cookie" header
    const setCookieHeader = loginPageResponse.headers.get('set-cookie');
    if (!setCookieHeader) {
      return [false, 'No Set-Cookie header returned from login page', null];
    }

    // 3. Prepare form data with the credentials AND the CSRF token
    const formData = new URLSearchParams();
    formData.append('username', data_s.username);
    formData.append('password', password_s);
    formData.append('_csrf', csrfToken);

    // 4. Send a POST request to the login URL with cookies + form data
    const loginResponse = await fetchTimeout(data_s.server_url + LOGIN_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Cookie: setCookieHeader, // Include the cookies from the initial GET
      },
      body: formData.toString(),
    });

    // 5. Check for login success by asking the server who we are: a
    // semantic check (the authenticated /whoami endpoint echoes the
    // username) instead of sniffing the followed login page's HTML for
    // localized error text.
    let loginSuccessful = false;
    try {
      const whoamiResponse = await fetchTimeout(
        data_s.server_url + WHOAMI_URL,
        {
          method: 'GET',
        },
      );
      if (whoamiResponse.ok) {
        const whoami = JSON.parse(await whoamiResponse.text());
        loginSuccessful = whoami && whoami.username === data_s.username;
      }
    } catch (_ignored) {
      loginSuccessful = false;
    }

    // 6. Proceed on a verified identity
    if (loginSuccessful) {
      // get CSRF token
      data_s.csrf_token = await getCSRFToken(data_s);

      // get server mode
      const serverModeResponse = await fetchTimeout(
        data_s.server_url + SERVER_MODE_URL,
        {
          method: 'GET',
        },
      );
      if (!serverModeResponse.ok) {
        return [
          false,
          'Login Successful but unable to get server mode; Status: ' +
            serverModeResponse.status,
          data_s,
        ];
      }
      const serverModeResponseText = await serverModeResponse.text();
      data_s.server_mode = String(JSON.parse(serverModeResponseText).mode);

      if (data_s.server_mode === 'P2P') {
        data_s.maxsize = '-1';

        // get stun url
        const stunUrlResponse = await fetchTimeout(
          data_s.server_url + STUN_URL,
          {
            method: 'GET',
          },
        );
        if (!stunUrlResponse.ok) {
          return [
            false,
            'Login Successful but unable to get stun url; Status: ' +
              stunUrlResponse.status,
            data_s,
          ];
        }
        const stunUrlResponseText = await stunUrlResponse.text();
        data_s.stun_url = String(JSON.parse(stunUrlResponseText).url);

        // convert server_url to websocket url
        data_s.websocket_url = await convertToWebSocketUrl(
          data_s.server_url,
          WEBSOCKET_ENDPOINT_P2P,
        );
      } else if (data_s.server_mode === 'P2S') {
        data_s.stun_url = '';

        // get max size
        const maxSizeResponse = await fetchTimeout(
          data_s.server_url + MAXSIZE_URL,
          {
            method: 'GET',
          },
        );
        if (!maxSizeResponse.ok) {
          return [
            false,
            'Login Successful but unable to get max size; Status: ' +
              maxSizeResponse.status,
            data_s,
          ];
        }
        const maxSizeResponseText = await maxSizeResponse.text();
        data_s.maxsize = String(JSON.parse(maxSizeResponseText).maxsize);

        // convert server_url to websocket url
        data_s.websocket_url = await convertToWebSocketUrl(
          data_s.server_url,
          WEBSOCKET_ENDPOINT,
        );
      }

      // Hash the password for encryption. The PBKDF2 input is the
      // EXPLICIT raw-password parameter, never closure state (which can be
      // empty on saved-credential logins). A legacy stored hash cannot
      // re-derive the key: keep the existing keystore key instead.
      if (data_s.cipher_enabled === 'true') {
        if (rawPasswordForKey !== null) {
          const hashResult = await hash(data_s, rawPasswordForKey);
          data_s = hashResult[2];
          if (!hashResult[0]) {
            return [
              false,
              'Login successful but error generating hash: ' + hashResult[1],
              data_s,
            ];
          }
          await NativeModules.NativeBridgeModule.storeE2EKey(
            hashResult[1].toString('base64'),
          );
        }
        data_s.hashed_password = '';
      }

      return [true, 'Login successful: ' + loginResponse.status, data_s];
    } else {
      return [false, 'Login failed: ' + loginResponse.status, data_s];
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      return [false, 'Error: Request timed out', data_s];
    }
    return [false, 'Error: ' + error, data_s];
  }
};

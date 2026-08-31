/**
 * Application-level constants shared by App.js, the UI page components and
 * the service module. Previously these lived inside the App() function,
 * reallocated on every render and invisible to unit tests.
 */

export const APP_VERSION = '3.2.0';
export const APP_NAME = 'ClipCascade';

export const MAX_LOGIN_AUTO_RETRY = 3; // Retry login attempts
export const FETCH_TIMEOUT = 5000; // 5 seconds

// Server endpoints
export const LOGIN_URL = '/login';
export const LOGOUT_URL = '/logout';
export const MAXSIZE_URL = '/max-size';
export const CSRF_URL = '/csrf-token';
export const SERVER_MODE_URL = '/server-mode';
export const WEBSOCKET_ENDPOINT = '/clipsocket';
export const VALIDATE_URL = '/validate-session';
export const WHOAMI_URL = '/whoami';
export const WEBSOCKET_ENDPOINT_P2P = '/p2psignaling';
export const STUN_URL = '/stun-url';

// Remote metadata
export const GITHUB_URL = 'https://github.com/Sathvik-Rao/ClipCascade';
export const RELEASE_URL =
  'https://github.com/Sathvik-Rao/ClipCascade/releases/latest';
export const HELP_URL = `${GITHUB_URL}/blob/main/README.md`;
export const VERSION_URL =
  'https://raw.githubusercontent.com/Sathvik-Rao/ClipCascade/main/version.json';
export const METADATA_URL =
  'https://raw.githubusercontent.com/Sathvik-Rao/ClipCascade/main/metadata.json';

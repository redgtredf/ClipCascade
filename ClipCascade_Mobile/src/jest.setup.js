/**
 * Jest setup: mocks for every native module in the import graph of App.js
 * (App -> StartForegroundService -> notifee/webrtc/aes-gcm/...). Without
 * these the component render test crashes inside the libraries' native
 * bindings long before any app code runs.
 */

// The app reads NativeModules.NativeBridgeModule (a Kotlin turbo-module
// with no JS fallback): install an inert stand-in on the real NativeModules
// object, which every later import of 'react-native' shares.
const { NativeModules } = require('react-native');
NativeModules.NativeBridgeModule = {
  storeE2EKey: jest.fn(),
  clearE2EKey: jest.fn(),
  clearCookies: jest.fn(),
  clearImageCache: jest.fn(),
  stopWorkManager: jest.fn(),
  getFlagsSync: jest.fn(() => '{}'),
  copyBase64ImageToClipboardUsingCache: jest.fn(),
  getFileAsBase64: jest.fn(),
  getFileName: jest.fn(),
};

jest.mock('@react-native-async-storage/async-storage', () =>
  require('@react-native-async-storage/async-storage/jest/async-storage-mock'),
);

jest.mock('@notifee/react-native', () => {
  const listeners = {};
  return {
    __esModule: true,
    default: {
      displayNotification: jest.fn(),
      cancelNotification: jest.fn(),
      cancelAllNotifications: jest.fn(),
      stopForegroundService: jest.fn(),
      openBatteryOptimizationSettings: jest.fn(),
      openPowerManagerSettings: jest.fn(),
      registerForegroundService: jest.fn(),
      onForegroundServiceEvent: jest.fn(),
      getPowerManagerInfo: jest.fn(),
      requestPermission: jest.fn(),
      createChannel: jest.fn(),
      // minimal event-emitter surface used by the app
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
      __listeners: listeners,
    },
    AndroidImportance: { HIGH: 1, DEFAULT: 2, LOW: 3, MIN: 4, NONE: 0 },
  };
});

// Native-backed libraries: any named export becomes an inert jest.fn().
jest.mock('react-native-webrtc', () => new Proxy({}, { get: () => jest.fn() }), {
  virtual: false,
});

jest.mock('react-native-aes-gcm-crypto', () => ({
  __esModule: true,
  default: new Proxy({}, { get: () => jest.fn() }),
}));

jest.mock('@react-native-module/pbkdf2', () => ({
  __esModule: true,
  pbkdf2: jest.fn(),
}));

jest.mock('@react-native-documents/picker', () => ({
  __esModule: true,
  pickDirectory: jest.fn(),
  isCancel: jest.fn(() => false),
}));

jest.mock('@react-native-clipboard/clipboard', () => ({
  __esModule: true,
  default: { setString: jest.fn(), getString: jest.fn() },
}));

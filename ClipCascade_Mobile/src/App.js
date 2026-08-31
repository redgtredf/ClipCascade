import { PermissionsAndroid, Alert, NativeModules, SafeAreaView, StatusBar, Text, View } from 'react-native';

import { useEffect, useRef, useState } from 'react';

import notifee from '@notifee/react-native';
import { pickDirectory, isCancel } from '@react-native-documents/picker';

import {
  setDataInAsyncStorage,
  getDataFromAsyncStorage,
} from './AsyncStorageManagement';
import StartForegroundService from './StartForegroundService';
const { requireSecureServerUrl } = require('./transportSecurity');
import {
  fetchTimeout,
  login,
  looksLikeSha3Hex,
  nextPollDelayMs,
  POLL_MIN_MS,
  stringToSHA3_512LowercaseHex,
  validateSession,
} from './appServices';
import {
  APP_NAME,
  APP_VERSION,
  LOGOUT_URL,
  MAX_LOGIN_AUTO_RETRY,
  METADATA_URL,
  VERSION_URL,
} from './appConfig';
import LoadingPage from './ui/LoadingPage';
import LoginPage from './ui/LoginPage';
import WebSocketPage from './ui/WebSocketPage';
import styles from './ui/styles';

/*
 * These files are part of the ClipCascade project.
 *
 * (file) android\app\src\main\java\com\clipcascade\AsyncStorageBridge.kt
 * (file) android\app\src\main\java\com\clipcascade\BootReceiver.kt
 * (file) android\app\src\main\java\com\clipcascade\ClipboardFloatingActivity.kt
 * (file) android\app\src\main\java\com\clipcascade\ClipboardListenerModule.kt
 * (file) android\app\src\main\java\com\clipcascade\ClipboardListenerPackage.kt
 * (file) android\app\src\main\java\com\clipcascade\HeadlessTaskService.kt
 * (file) android\app\src\main\java\com\clipcascade\MainActivity.kt
 * (file) android\app\src\main\java\com\clipcascade\MainApplication.kt
 * (file) android\app\src\main\java\com\clipcascade\NativeBridgeModule.kt
 * (file) android\app\src\main\java\com\clipcascade\NativeBridgePackage.kt
 * (file) android\app\src\main\java\com\clipcascade\ScheduleService.kt
 * (file) android\app\src\main\AndroidManifest.xml
 * (folder) android\app\src\main\res\
 * (file) android\app\build.gradle
 * (file) android\app\my-upload-key.keystore
 * (file) android\gradle.properties
 * (file) AsyncStorageManagement.js
 * (file) HeadlessTask.js
 * (file) index.js
 * (file) StartForegroundService.js
 */

// Main App: owns the state, effects and handlers; the pages and the
// stateless services live in ./ui/* and ./appServices.
export default function App() {
  const { NativeBridgeModule } = NativeModules;

  const isMountedRef = useRef(true);

  const [newVersionAvailable, setNewVersionAvailable] = useState([false, '']);
  const [donateUrl, setDonateUrl] = useState(null);

  // Request permissions for notifications (once on mount; a bare call in
  // the component body is a side effect that fired on every render)
  useEffect(() => {
    PermissionsAndroid.request(PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS);
  }, []);

  //data hook
  const [data, setData] = useState({
    cipher_enabled: 'true',
    server_url: 'https://localhost:8080',
    websocket_url: '',
    username: '',
    hashed_password: '',
    csrf_token: '',
    maxsize: '',
    hash_rounds: '664937',
    salt: '',
    save_password: 'false',
    max_clipboard_size_local_limit_bytes: '',
    relaunch_on_boot: 'false',
    enable_websocket_status_notification: 'false',
    enable_periodic_checks: 'true',
    enable_image_sharing: 'true',
    enable_file_sharing: 'true',
    server_mode: 'P2S',
    stun_url: '',
  });

  /*
   * Virtual/Helper Async Storage Fields:
   *
   * downloadFiles, filesAvailableToDownload, dirPath, wsStatusMessage, p2pStatusMessage, echo, wsIsRunning,
   * enableWSButton, foreground_service_stopped_running, password, wsForegroundServiceTerminated
   */

  // get data from async storage
  const getAsyncStorage = async () => {
    try {
      let data_s = { ...data };
      for (const key in data_s) {
        const value = await getDataFromAsyncStorage(key);
        //value === null means there is no key/data in async storage
        if (value !== null) {
          data_s[key] = value;
        }
      }
      return data_s;
    } catch (e) {
      throw e;
    }
  };

  //set data in async storage
  const setAsyncStorage = async data_s => {
    try {
      for (const key in data_s) {
        if (data_s[key] === null) {
          data_s[key] = '';
        }
        await setDataInAsyncStorage(key, data_s[key]);
      }
    } catch (e) {
      throw e;
    }
  };

  const clearFiles = async () => {
    try {
      await setDataInAsyncStorage('filesAvailableToDownload', 'false');
      await setDataInAsyncStorage('downloadFiles', 'false');
      await setDataInAsyncStorage('dirPath', '');
      await notifee.cancelNotification(
        'ClipCascade_Download_Files_Notification_Id',
      );
      setEnableFilesDownloadButton(false);
    } catch (e) {
      throw e;
    }
  };

  const [initError, setInItError] = useState([false, '']);
  useEffect(() => {
    // initialize
    const init = async () => {
      try {
        // enable websocket button
        await setDataInAsyncStorage('enableWSButton', 'true');

        // start polling UI flags
        isMountedRef.current = true;
        pollUIFlags();

        // get data from async storage and initialize data hook
        let data_s = await getAsyncStorage();
        setData(data_s);

        // get foreground service status from work manager
        let foregroundServiceStoppedRunning = await getDataFromAsyncStorage(
          'foreground_service_stopped_running',
        );

        // Check if websocket is running, if so directly navigate to next page
        let wsIsRunning_s = await getDataFromAsyncStorage('wsIsRunning');
        if (wsIsRunning_s === null) {
          wsIsRunning_s = 'false';
        }
        // check if foreground service is running
        if (wsIsRunning_s === 'true') {
          let foregroundServiceIsActive = false;

          setLoadingPageMessage('Checking foreground service...');
          await setDataInAsyncStorage('echo', 'ping');
          let iterate = 35; //3500 ms
          while (iterate > 0) {
            await new Promise(resolve => setTimeout(resolve, 100)); //100 ms
            const echo = await getDataFromAsyncStorage('echo');
            if (echo && echo === 'pong') {
              foregroundServiceIsActive = true;
              break;
            }
            iterate--;
          }
          if (!foregroundServiceIsActive) {
            wsIsRunning_s = 'false';
            await clearFiles();
            await setDataInAsyncStorage(
              'wsStatusMessage',
              '⚠️ Foreground service stopped running',
            );
          }
        }
        setWsIsRunning(wsIsRunning_s);

        if (wsIsRunning_s === 'true') {
          //enable websocket page
          setEnableLoadingPage(false);
          setEnableWSPage(true);
        } else {
          await setDataInAsyncStorage('p2pStatusMessage', '');
          //validate session
          setLoadingPageMessage('Verifying Session...');
          const validResult = await validateSession(data_s);
          setEnableLoadingPage(false);
          if (validResult[0]) {
            //enable websocket page
            setEnableWSPage(true);
            setDataInAsyncStorage('wsIsRunning', 'false');
            // start foreground service (work manager notification click handler)
            if (
              foregroundServiceStoppedRunning &&
              foregroundServiceStoppedRunning === 'true'
            ) {
              foregroundService();
            }
          } else {
            //enable login page
            await setDataInAsyncStorage('wsStatusMessage', '');
            setEnableLoginPage(true);
            setDataInAsyncStorage('wsIsRunning', 'false');
            if (data_s.save_password === 'true') {
              const pass = await getDataFromAsyncStorage('password');
              if (
                pass !== null &&
                pass !== '' &&
                data_s.cipher_enabled !== 'true'
              ) {
                setPassword(pass);
                handleLogin(pass, data_s);
              }
            }
          }
        }

        // check for new version
        try {
          const response = await fetchTimeout(VERSION_URL);
          if (!response.ok) {
            throw new Error('Network response was not ok');
          }
          const data = await response.json();
          if (data && data.android !== APP_VERSION) {
            setNewVersionAvailable([true, data.android]);
          }
        } catch (e) {
          // Silent catch
        }

        try {
          const response = await fetchTimeout(METADATA_URL);
          if (!response.ok) {
            throw new Error('Network response was not ok');
          }
          const data = await response.json();
          if (data) {
            setDonateUrl(data.funding);
          }
        } catch (e) {
          // Silent catch
        }
      } catch (e) {
        setInItError([true, e.message]);
      } finally {
        // reset foreground service work manager status
        await setDataInAsyncStorage(
          'foreground_service_stopped_running',
          'false',
        );
      }
    };
    init();

    // cleanup
    return () => {
      isMountedRef.current = false;
      const clearWSStatusMessage = async () => {
        try {
          const wsIsRunning_s = await getDataFromAsyncStorage('wsIsRunning');
          if (wsIsRunning_s !== null && wsIsRunning_s === 'false') {
            await setDataInAsyncStorage('wsStatusMessage', '');
            await setDataInAsyncStorage('p2pStatusMessage', '');
          }
        } catch (error) {
          // Silent catch
        }
      };
      clearWSStatusMessage();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* Login Page Handlers */
  // State to manage login page visibility
  const [enableLoginPage, setEnableLoginPage] = useState(false);

  // State to manage visibility of extra config fields
  const [showExtraConfig, setShowExtraConfig] = useState(false);

  // State to manage login status message
  const [loginStatusMessage, setLoginStatusMessage] = useState('');

  // password
  const [password, setPassword] = useState('');

  // Function to handle input changes in the login form
  const handleInputChange = (field, value) => {
    try {
      setData(prevData => ({
        ...prevData,
        [field]: value,
      }));
    } catch (e) {
      setLoginStatusMessage('❌ Error: ' + e);
    }
  };

  // Function to handle login button press
  const handleLogin = async (pass = null, data_s_c = null) => {
    try {
      setLoginStatusMessage('⌛ Please wait...');

      // clear cookies if any
      await NativeBridgeModule.clearCookies();

      let password_s = null;
      let rawPasswordForKey = null;
      if (pass === null) {
        rawPasswordForKey = password; // the raw text from the input
        password_s = await stringToSHA3_512LowercaseHex(password);
      } else if (looksLikeSha3Hex(pass)) {
        // legacy storage kept the sha3 hex: use it for the POST, but the raw
        // password is unknowable, so the PBKDF2 key cannot be re-derived
        // (the existing keystore key stays in place).
        password_s = pass;
      } else {
        // stored raw (current format): hash it for the POST, keep the raw
        // for key derivation
        rawPasswordForKey = pass;
        password_s = await stringToSHA3_512LowercaseHex(pass);
      }

      // synchronous data instead of state hook data for instant updates
      let data_s;
      if (data_s_c !== null) {
        data_s = data_s_c;
      } else {
        data_s = { ...data };
      }

      data_s.server_url = requireSecureServerUrl(data_s.server_url);

      let iteration = 0;
      let loginResult;
      do {
        iteration++;
        loginResult = await login(data_s, password_s, rawPasswordForKey);
      } while (!loginResult[0] && iteration < MAX_LOGIN_AUTO_RETRY);

      data_s = loginResult[2];
      if (!loginResult[0]) {
        setPassword('');
        setLoginStatusMessage(['❌ ', loginResult[1]].join(''));
      } else {
        setLoginStatusMessage(['✅ ', loginResult[1]].join(''));

        // Stop periodic checks work manager notification (if disabled)
        if (data_s.enable_periodic_checks === 'false') {
          NativeBridgeModule.stopWorkManager();
        }

        // Save password in async storage. The RAW password is stored so a
        // saved-credential login can re-derive the PBKDF2 key (older builds
        // stored the sha3 hex, which could not).
        if (data_s.save_password === 'true') {
          await setDataInAsyncStorage(
            'password',
            rawPasswordForKey !== null ? rawPasswordForKey : password_s,
          );
        }
        // Remove password from memory
        setPassword('');
        password_s = '';

        // Save data in async storage
        await setAsyncStorage(data_s);

        // Save data_s in data state hook
        setData(data_s);

        // Navigation to the websocket screen
        setEnableLoginPage(false);
        setEnableWSPage(true);
        setWsPageMessage('');
        setWsPageP2PMessage('');
      }
    } catch (e) {
      setLoginStatusMessage('❌ Error: ' + e);
    }
  };

  /* Websocket Page Handlers */
  // State to manage websocket page visibility
  const [enableWSPage, setEnableWSPage] = useState(false);

  // State to manage websocket status
  const [wsIsRunning, setWsIsRunning] = useState('false');

  // State to manage websocket page message
  const [wsPageMessage, setWsPageMessage] = useState('');

  // State to manage websocket page p2p message
  const [wsPageP2PMessage, setWsPageP2PMessage] = useState('');

  // files download button
  const [enableFilesDownloadButton, setEnableFilesDownloadButton] =
    useState(false);

  // download files
  const downloadFiles = async () => {
    try {
      if (
        (await getDataFromAsyncStorage('filesAvailableToDownload')) === 'true'
      ) {
        const res = await pickDirectory();
        await setDataInAsyncStorage('dirPath', res.uri);
        await setDataInAsyncStorage('downloadFiles', 'true');
      } else {
        setEnableFilesDownloadButton(false);
      }
    } catch (e) {
      if (!isCancel(e)) {
        Alert.alert('Error', 'Unknown error: ' + JSON.stringify(e));
      }
    }
  };

  /* Service + poll handlers */

  function sleep(ms) {
    return new Promise(res => setTimeout(res, ms));
  }

  async function pollUIFlags() {
    const POLL_KEYS = [
      'wsIsRunning',
      'wsStatusMessage',
      'server_mode',
      'p2pStatusMessage',
      'filesAvailableToDownload',
    ];

    let previousJson = null;
    let delayMs = POLL_MIN_MS;
    while (isMountedRef.current) {
      const json = NativeBridgeModule.getFlagsSync(POLL_KEYS);
      const latest = JSON.parse(json);

      if (latest.wsIsRunning === 'true') {
        // Websocket status message
        const msg1 = latest.wsStatusMessage;
        if (msg1 !== null && msg1 !== '') {
          setWsPageMessage(msg1);
        }

        if (latest.server_mode === 'P2P') {
          const msg2 = latest.p2pStatusMessage;
          if (msg2 !== null) {
            setWsPageP2PMessage(msg2);
          }
        }

        // Files available to download
        if (latest.filesAvailableToDownload === 'true') {
          setEnableFilesDownloadButton(true);
        } else {
          setEnableFilesDownloadButton(false);
        }
      }

      // Any flag change snaps the cadence back to the minimum; an unchanged
      // snapshot relaxes it, so the bridge is polled hard only while the
      // state is actually moving.
      delayMs = nextPollDelayMs(delayMs, json !== previousJson);
      previousJson = json;
      await sleep(delayMs);
    }
  }

  const onDisplayNotification = async () => {
    try {
      // remove work manager notification if exists
      await notifee.cancelAllNotifications();

      // stop foreground service(if any)
      await notifee.stopForegroundService();

      // start foreground service
      const result = await StartForegroundService();
      if (result[0] === false) {
        throw result[1];
      }
    } catch (e) {
      throw e;
    }
  };

  // Foreground service handler
  const foregroundService = async () => {
    try {
      if ((await getDataFromAsyncStorage('enableWSButton')) === 'true') {
        await setDataInAsyncStorage('enableWSButton', 'false');
        setWsPageMessage('');
        setWsPageP2PMessage('');
        await clearFiles();
        const wsIsRunning_s = wsIsRunning === 'true' ? 'false' : 'true'; // toggle
        await setDataInAsyncStorage('wsForegroundServiceTerminated', 'false');
        await setDataInAsyncStorage('wsIsRunning', wsIsRunning_s);
        if (wsIsRunning_s === 'true') {
          //start foreground service
          await setDataInAsyncStorage('wsStatusMessage', '');
          await setDataInAsyncStorage('p2pStatusMessage', '');
          setWsPageMessage('🚀 Starting foreground service...');
          await onDisplayNotification();
        } else {
          // wait for 1 sec so that foreground service can be terminated
          setWsPageMessage('⌛ Stopping foreground service...');
          while (
            (await getDataFromAsyncStorage('wsForegroundServiceTerminated')) ===
            'false'
          ) {
            await new Promise(resolve => setTimeout(resolve, 100)); //100 ms
          }
          await notifee.cancelAllNotifications();
          setWsPageMessage('');
          setWsPageP2PMessage('');
        }
        await NativeBridgeModule.clearImageCache();

        setWsIsRunning(wsIsRunning_s);
      }
    } catch (error) {
      setWsPageMessage('❌ Error: ' + error);
    } finally {
      await setDataInAsyncStorage('enableWSButton', 'true');
    }
  };

  // Logout
  const logout = async () => {
    try {
      setWsPageMessage('⌛ Please wait...');
      await setDataInAsyncStorage('password', '');
      await NativeBridgeModule.clearE2EKey();
      if (wsIsRunning === 'true') {
        await setDataInAsyncStorage('wsIsRunning', 'false');
        setWsIsRunning('false');
      }

      const formData = new URLSearchParams();
      formData.append('_csrf', await getDataFromAsyncStorage('csrf_token'));

      const response = await fetchTimeout(data.server_url + LOGOUT_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: formData.toString(),
      });

      if (response.status == 204) {
        setWsPageMessage('✅ Logout successful: ' + response.status);
      } else {
        setWsPageMessage('❌ Logout failed: ' + response.status);
      }

      await setDataInAsyncStorage('csrf_token', '');
      setEnableWSPage(false);
      setEnableLoginPage(true);
      setLoginStatusMessage('');

      // clear cookies if any
      NativeBridgeModule.clearCookies();
    } catch (error) {
      if (error.name === 'AbortError') {
        setWsPageMessage('❌ Error: Request timed out');
      } else {
        setWsPageMessage('❌ Error: ' + error);
      }
    }
  };

  /* Loading Page Handlers */
  // State to manage loading page visibility
  const [enableLoadingPage, setEnableLoadingPage] = useState(true);

  // State to manage loading page message
  const [loadingPageMessage, setLoadingPageMessage] = useState('Loading...');

  // view
  if (initError[0]) {
    return (
      <SafeAreaView
        style={{
          flex: 1,
          paddingTop: StatusBar.currentHeight,
          justifyContent: 'center',
          alignItems: 'center',
        }}
      >
        <View style={styles.loadingContainer}>
          <Text style={styles.appTitle}>{APP_NAME}</Text>
          <View style={styles.loadingBottomContainer}>
            <Text style={styles.loadingText}>Init Error: {initError[1]}</Text>
          </View>
        </View>
      </SafeAreaView>
    );
  }
  return (
    <SafeAreaView
      style={{
        flex: 1,
        paddingTop: StatusBar.currentHeight,
      }}
    >
      {/* Loading Page */}
      {enableLoadingPage && <LoadingPage message={loadingPageMessage} />}

      {/* Login Page */}
      {enableLoginPage && (
        <LoginPage
          data={data}
          password={password}
          onPasswordChange={setPassword}
          onFieldChange={handleInputChange}
          onLogin={handleLogin}
          loginStatusMessage={loginStatusMessage}
          showExtraConfig={showExtraConfig}
          onToggleExtraConfig={setShowExtraConfig}
          donateUrl={donateUrl}
        />
      )}

      {/* websocket page */}
      {enableWSPage && (
        <WebSocketPage
          wsIsRunning={wsIsRunning}
          onToggleService={foregroundService}
          onLogout={logout}
          wsPageMessage={wsPageMessage}
          wsPageP2PMessage={wsPageP2PMessage}
          enableFilesDownloadButton={enableFilesDownloadButton}
          onDownloadFiles={downloadFiles}
          newVersionAvailable={newVersionAvailable}
          donateUrl={donateUrl}
          serverUrl={data.server_url}
        />
      )}
    </SafeAreaView>
  );
}

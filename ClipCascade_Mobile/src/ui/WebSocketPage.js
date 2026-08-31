import { Linking, ScrollView, Text, TouchableOpacity, View } from 'react-native';

import { APP_VERSION, RELEASE_URL } from '../appConfig';
import Footer from './Footer';
import InstructionsSection from './InstructionsSection';
import styles from './styles';

/**
 * The running-services page: start/stop the foreground service, logout,
 * live status messages, the pending file-download action and the update
 * notice. Presentational; all behaviour arrives via callbacks.
 */
export default function WebSocketPage({
  wsIsRunning,
  onToggleService,
  onLogout,
  wsPageMessage,
  wsPageP2PMessage,
  enableFilesDownloadButton,
  onDownloadFiles,
  newVersionAvailable,
  donateUrl,
  serverUrl,
}) {
  return (
    <ScrollView contentContainerStyle={styles.container}>
      <Text style={styles.appTitle}>ClipCascade</Text>
      <View style={styles.container}>
        <TouchableOpacity
          style={[
            styles.loginButton,
            {
              backgroundColor: wsIsRunning === 'true' ? '#800020' : 'green',
            },
          ]}
          onPress={onToggleService}
        >
          <Text style={styles.loginButtonText}>
            {wsIsRunning === 'true' ? 'Stop' : 'Start'}
          </Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[styles.loginButton, { backgroundColor: '#800020' }]}
          onPress={onLogout}
        >
          <Text style={styles.loginButtonText}>Logout</Text>
        </TouchableOpacity>
        {/* Display websocket status message */}
        {wsPageMessage !== '' && (
          <Text style={styles.message}>{wsPageMessage}</Text>
        )}
        {/* Display p2p status message */}
        {wsPageP2PMessage !== '' && (
          <Text style={styles.message}>{wsPageP2PMessage}</Text>
        )}
        {/* File download button */}
        {enableFilesDownloadButton && enableFilesDownloadButton === true && (
          <TouchableOpacity
            style={[styles.loginButton, { backgroundColor: '#4bab4e' }]}
            onPress={onDownloadFiles}
          >
            <Text style={styles.loginButtonText}>📥 Download File(s)</Text>
          </TouchableOpacity>
        )}
        {/* new version display message */}
        {newVersionAvailable[0] && (
          <TouchableOpacity
            onPress={() => Linking.openURL(RELEASE_URL)}
            style={{ marginTop: 10 }}
          >
            <Text
              style={[
                styles.message,
                {
                  color: '#008080',
                  fontWeight: 'bold',
                  textDecorationLine: 'underline',
                },
              ]}
            >
              New version available! 🚀 Click here to update ({APP_VERSION} ➞{' '}
              {newVersionAvailable[1]})
            </Text>
          </TouchableOpacity>
        )}
        <InstructionsSection />
      </View>
      {/* Footer */}
      <Footer donateUrl={donateUrl} homepageUrl={serverUrl} />
    </ScrollView>
  );
}

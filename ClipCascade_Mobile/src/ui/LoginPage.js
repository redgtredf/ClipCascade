import { ScrollView, Text, TextInput, TouchableOpacity, View } from 'react-native';

import CheckBox from '@react-native-community/checkbox';

import Footer from './Footer';
import styles from './styles';

/**
 * The login form. Presentational: every mutation flows back up through the
 * callbacks. `data` carries the form fields as strings (the app stores all
 * configuration as stringified values).
 */
export default function LoginPage({
  data,
  password,
  onPasswordChange,
  onFieldChange,
  onLogin,
  loginStatusMessage,
  showExtraConfig,
  onToggleExtraConfig,
  donateUrl,
}) {
  return (
    <ScrollView contentContainerStyle={styles.container}>
      <Text style={styles.appTitle}>ClipCascade</Text>
      <View style={styles.row}>
        <Text style={styles.label}>Username:</Text>
        <TextInput
          style={styles.input}
          value={data.username}
          onChangeText={text => onFieldChange('username', text)}
          autoCapitalize="none"
        />
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Password:</Text>
        <TextInput
          style={styles.input}
          value={password}
          onChangeText={text => onPasswordChange(text)}
          secureTextEntry
          autoCapitalize="none"
        />
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Server URL:</Text>
        <TextInput
          style={styles.input}
          value={data.server_url}
          onChangeText={text => onFieldChange('server_url', text.trim())}
          autoCapitalize="none"
        />
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Enable Encryption (recommended):</Text>
        <CheckBox
          value={data.cipher_enabled === 'true' ? true : false}
          onValueChange={newValue =>
            onFieldChange('cipher_enabled', String(newValue))
          }
        />
      </View>

      {/* Display login status message */}
      {loginStatusMessage !== '' && (
        <Text style={styles.message}>{loginStatusMessage}</Text>
      )}

      {/* Login Button */}
      <TouchableOpacity
        style={styles.loginButton}
        onPress={() => {
          onLogin(null, null);
        }}
      >
        <Text style={styles.loginButtonText}>Login</Text>
      </TouchableOpacity>

      {/* Toggle Extra Config as Text */}
      <TouchableOpacity onPress={() => onToggleExtraConfig(!showExtraConfig)}>
        <Text style={styles.linkText}>
          {showExtraConfig ? 'Hide Extra Config' : 'Enable Extra Config'}
        </Text>
      </TouchableOpacity>

      {/* Extra Config Fields (conditionally rendered) */}
      {showExtraConfig && (
        <>
          <View style={styles.row}>
            <Text style={styles.label}>Hash Rounds:</Text>
            <TextInput
              style={styles.input}
              value={data.hash_rounds}
              onChangeText={text =>
                onFieldChange(
                  'hash_rounds',
                  isNaN(Number(text)) ? data.hash_rounds : text.trim(),
                )
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>Salt:</Text>
            <TextInput
              style={styles.input}
              value={data.salt}
              onChangeText={text => onFieldChange('salt', text)}
              autoCapitalize="none"
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>
              Store Password Locally (not recommended; only works if
              encryption is disabled):
            </Text>
            <CheckBox
              value={data.save_password === 'true' ? true : false}
              onValueChange={newValue =>
                onFieldChange('save_password', String(newValue))
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>
              Maximum Clipboard Size Local Limit (in bytes):
            </Text>
            <TextInput
              style={styles.input}
              value={data.max_clipboard_size_local_limit_bytes}
              onChangeText={text =>
                onFieldChange(
                  'max_clipboard_size_local_limit_bytes',
                  isNaN(Number(text))
                    ? data.max_clipboard_size_local_limit_bytes
                    : text.trim(),
                )
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>
              Run on system startup (disable if the READ_LOGS permission is
              granted):
            </Text>
            <CheckBox
              value={data.relaunch_on_boot === 'true' ? true : false}
              onValueChange={newValue =>
                onFieldChange('relaunch_on_boot', String(newValue))
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>
              Enable WebSocket Status Notification:
            </Text>
            <CheckBox
              value={
                data.enable_websocket_status_notification === 'true'
                  ? true
                  : false
              }
              onValueChange={newValue =>
                onFieldChange(
                  'enable_websocket_status_notification',
                  String(newValue),
                )
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>Enable Periodic Checks:</Text>
            <CheckBox
              value={data.enable_periodic_checks === 'true' ? true : false}
              onValueChange={newValue =>
                onFieldChange('enable_periodic_checks', String(newValue))
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>Enable Image Sharing:</Text>
            <CheckBox
              value={data.enable_image_sharing === 'true' ? true : false}
              onValueChange={newValue =>
                onFieldChange('enable_image_sharing', String(newValue))
              }
            />
          </View>
          <View style={styles.row}>
            <Text style={styles.label}>Enable File Sharing:</Text>
            <CheckBox
              value={data.enable_file_sharing === 'true' ? true : false}
              onValueChange={newValue =>
                onFieldChange('enable_file_sharing', String(newValue))
              }
            />
          </View>
        </>
      )}
      <Footer donateUrl={donateUrl} />
    </ScrollView>
  );
}
